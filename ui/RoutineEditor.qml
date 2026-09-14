import QtQuick
import QtQuick.Controls as Controls
import qs.Commons
import qs.Ui

// Creates one routine. Name, prompt, and schedule are the whole form; the
// working folder, model, tools, and permission mode are prefilled from the
// service and stay folded away unless the user asks for them. The next run
// times appear on their own as the schedule changes, so there is nothing to
// press before saving.
Column {
  id: root
  objectName: "routineEditor"
  spacing: Style.space(10)
  property var defaults: ({})
  property var client: null
  property bool connected: true
  property string error: ""
  property string previewText: ""
  property string previewKey: ""
  property string mode: "Daily"
  property string permissionMode: ""
  property bool showMore: false
  readonly property color dim: Qt.darker(Color.foreground, 1.4)
  readonly property bool timed: mode === "Daily" || mode === "Weekdays" || mode === "Weekly"
  signal revealRequested(var item)
  signal saved(var routine)
  signal canceled()

  onErrorChanged: if (error !== "") Qt.callLater(function() { root.revealRequested(saveButton) })

  function cronExpression() {
    if (mode === "Manual") return null
    if (mode === "Advanced") return advanced.text
    var pieces = time.text.split(":")
    var hour = Number(pieces[0]), minute = Number(pieces[1])
    if (!/^\d\d:\d\d$/.test(time.text) || hour > 23 || minute > 59) return "invalid time"
    return minute + " " + hour + " * * " + (mode === "Weekdays" ? "1-5" : mode === "Weekly" ? weekday.value : "*")
  }
  function draft() {
    return {name: name.text, prompt: prompt.text, model: model.text, cwd: folder.text,
      schedule_kind: mode === "Manual" ? "manual" : "cron", cron: cronExpression(),
      timezone: mode === "Manual" ? null : timezone.text,
      tools: tools.text, permission_mode: permissionMode || root.defaults.permission_mode || "bypassPermissions",
      mcp_config: null, env_passthrough: []}
  }
  function scheduleChanged() {
    previewText = ""
    previewTimer.restart()
  }
  function preview() {
    previewTimer.stop()
    if (mode === "Manual") { previewText = "Runs only when you ask."; return }
    if (cronExpression() === "invalid time") { previewText = "Enter the time as HH:MM, for example 09:00."; return }
    var params = {cron: cronExpression(), timezone: timezone.text}
    if (client.busy) { previewTimer.restart(); return }
    if (client.request("preview_schedule", params)) {
      previewKey = JSON.stringify(params)
      previewText = "Checking the next run times..."
    }
  }
  function save() {
    if (cronExpression() === "invalid time") { error = "Enter the time as HH:MM, for example 09:00."; return }
    error = ""
    client.request("create_routine", draft())
  }
  function focusFirst() { name.forceActiveFocus() }
  function localTime(iso) {
    // 2026-09-14T09:00:00-04:00 -> Mon 14 Sep 09:00
    var d = new Date(iso.slice(0, 19))
    return isNaN(d) ? iso : Qt.formatDateTime(d, "ddd d MMM HH:mm")
  }

  Timer { id: previewTimer; interval: 400; onTriggered: root.preview() }
  Component.onCompleted: scheduleChanged()

  Connections {
    target: root.client
    function onResult(op, params, data) {
      if (!root.visible) return
      if (op === "preview_schedule" && JSON.stringify(params) === root.previewKey) {
        if (params.cron !== root.cronExpression() || params.timezone !== timezone.text) { root.scheduleChanged(); return }
        root.previewText = "Next runs · " + data.timezone + "\n" + data.occurrences.map(function(t) { return root.localTime(t.local) }).join("\n")
      } else if (op === "create_routine") root.saved(data.routine)
    }
    function onFailure(op, code, message) {
      if (!root.visible) return
      if (op === "preview_schedule") root.previewText = message
      else root.error = message
    }
  }

  Column {
    width: parent.width
    spacing: Style.space(2)
    Text {
      text: "New routine"
      color: Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      font.bold: true
    }
    Caption { text: "A saved prompt that Claude Code runs on a schedule, with its usual tools." }
  }

  PanelSeparator { width: parent.width }

  FieldLabel { text: "Name" }
  TextField {
    id: name
    objectName: "field_name"
    width: parent.width
    placeholderText: "Morning repository report"
    Accessible.name: "Routine name"
    onActiveFocusChanged: if (activeFocus) root.revealRequested(name)
  }

  FieldLabel { text: "Prompt" }
  BorderSurface {
    id: promptFrame
    width: parent.width
    height: Style.space(120)
    radius: Style.cornerRadius
    readonly property var spec: Border.controlSpec(prompt.activeFocus ? "focus" : (promptHover.hovered ? "hover-cursor" : "normal"), Color.foreground, Color.accent)
    color: Style.controlFill(prompt.activeFocus, promptHover.hovered, Color.foreground, Color.accent)
    borderSpec: spec
    HoverHandler { id: promptHover }
    Controls.ScrollView {
      anchors.fill: parent
      anchors.margins: Border.top(promptFrame.spec)
      clip: true
      Controls.TextArea {
        id: prompt
        objectName: "field_prompt"
        placeholderText: "What should Claude Code do, and what should it reply with when done?"
        placeholderTextColor: Qt.darker(Color.foreground, 1.6)
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
        Accessible.name: "Routine prompt"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(promptFrame)
        // A text area swallows Tab as input. Hand it to the focus chain instead.
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Tab) { prompt.nextItemInFocusChain(true).forceActiveFocus(); event.accepted = true }
          else if (event.key === Qt.Key_Backtab) { prompt.nextItemInFocusChain(false).forceActiveFocus(); event.accepted = true }
        }
      }
    }
  }

  Dropdown {
    id: schedule
    width: parent.width
    label: "Schedule"
    value: root.mode
    options: ["Daily", "Weekdays", "Weekly", "Manual", "Advanced"]
    onChanged: function(value) { root.mode = value; root.scheduleChanged() }
  }

  Row {
    visible: root.timed
    width: parent.width
    spacing: Style.space(10)
    Column {
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Time" }
      TextField {
        id: time
        objectName: "field_time"
        width: Style.space(90)
        text: "09:00"
        placeholderText: "HH:MM"
        Accessible.name: "Schedule time"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(time)
        onTextChanged: root.scheduleChanged()
      }
    }
    Dropdown {
      id: weekday
      visible: root.mode === "Weekly"
      width: parent.width - time.width - parent.spacing
      label: "Day"
      value: "1"
      options: [{value:"1",label:"Monday"},{value:"2",label:"Tuesday"},{value:"3",label:"Wednesday"},{value:"4",label:"Thursday"},{value:"5",label:"Friday"},{value:"6",label:"Saturday"},{value:"0",label:"Sunday"}]
      onChanged: function(value) { weekday.value = value; root.scheduleChanged() }
    }
  }

  Column {
    visible: root.mode === "Advanced"
    width: parent.width
    spacing: Style.spacing.labelGap
    FieldLabel { text: "Cron expression" }
    TextField {
      id: advanced
      objectName: "field_advanced"
      width: parent.width
      text: "0 9 * * *"
      placeholderText: "minute hour day month weekday"
      Accessible.name: "Five field cron"
      onActiveFocusChanged: if (activeFocus) root.revealRequested(advanced)
      onTextChanged: root.scheduleChanged()
    }
  }

  Column {
    visible: root.mode !== "Manual"
    width: parent.width
    spacing: Style.spacing.labelGap
    FieldLabel { text: "Time zone" }
    TextField {
      id: timezone
      objectName: "field_timezone"
      width: parent.width
      text: root.defaults.timezone || "UTC"
      placeholderText: "America/New_York"
      Accessible.name: "Schedule timezone"
      onActiveFocusChanged: if (activeFocus) root.revealRequested(timezone)
      onTextChanged: root.scheduleChanged()
    }
  }

  Caption { id: previewLabel; text: root.previewText; visible: text !== "" }

  Button {
    text: root.showMore ? "Hide folder, model, and tools" : "Working folder, model, and tools"
    iconText: root.showMore ? "󰅃" : "󰅀"
    fontSize: Style.font.bodySmall
    focusable: true
    horizontalPadding: 0
    onClicked: root.showMore = !root.showMore
  }
  Column {
    visible: root.showMore
    width: parent.width
    spacing: Style.space(10)
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Working folder" }
      TextField {
        id: folder
        objectName: "field_folder"
        width: parent.width
        text: root.defaults.cwd || ""
        Accessible.name: "Working folder"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(folder)
      }
      Caption { text: "Claude Code starts in this folder and can change what is in it." }
    }
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Model" }
      TextField {
        id: model
        objectName: "field_model"
        width: parent.width
        text: root.defaults.model || "claude-sonnet-5"
        Accessible.name: "Claude model"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(model)
      }
      Caption { text: (root.defaults.verified_models || []).indexOf(model.text) >= 0 ? "This model has been verified with Omakron." : "This model is unverified. Runs will not fall back to another model." }
    }
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Tools" }
      TextField {
        id: tools
        objectName: "field_tools"
        width: parent.width
        text: root.defaults.tools || "default"
        placeholderText: "default"
        Accessible.name: "Claude Code tools"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(tools)
      }
      Caption { text: "\"default\" is Claude Code's full tool set. Leave it empty for no tools, or list tool names separated by commas, such as Read,Edit,Bash." }
    }
    Dropdown {
      id: permission
      objectName: "field_permission"
      width: parent.width
      label: "Permission mode"
      value: root.permissionMode || root.defaults.permission_mode || "bypassPermissions"
      options: root.defaults.permission_modes || ["bypassPermissions", "acceptEdits", "auto", "dontAsk", "manual", "plan"]
      onChanged: function(value) { root.permissionMode = value }
    }
    Caption { text: "Nobody answers prompts during a scheduled run. bypassPermissions lets the run act on its own; a narrower mode denies whatever would have asked." }
  }

  PanelSeparator { width: parent.width }

  Caption { text: (root.defaults.policy || "Claude Code runs with its usual tools and no permission prompts.") + " New routines start paused so you can review them first." }
  Caption { text: root.error; visible: text !== ""; color: Color.urgent }

  Row {
    spacing: Style.space(8)
    Button {
      id: saveButton
      objectName: "saveRoutine"
      text: "Save routine"
      focusable: true
      bordered: true
      enabled: root.connected && !root.client.busy
      onActiveFocusChanged: if (activeFocus) root.revealRequested(this)
      onClicked: root.save()
    }
    Button { objectName: "cancelEditor"; text: "Cancel"; focusable: true; onClicked: root.canceled() }
  }

  component FieldLabel: Text {
    textFormat: Text.PlainText
    color: root.dim
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }
  component Caption: Text {
    width: root.width
    wrapMode: Text.Wrap
    textFormat: Text.PlainText
    color: root.dim
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }
}
