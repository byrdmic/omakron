import QtQuick
import QtQuick.Controls as Controls
import Quickshell.Io
import qs.Commons
import qs.Ui

// Creates one routine, one decision at a time. First: write your own, or
// load a skill folder. Loading shows a picker that fills a path field the
// user can also type into; once the SKILL.md is read, the form opens with the
// name and prompt filled in, and the service reads that file again at every
// run. The form itself is name, prompt, and schedule; the working folder,
// model, tools, and permission mode are prefilled from the service and stay
// folded away unless asked for. The next run times appear on their own as the
// schedule changes, so there is nothing to press before saving.
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
  property string step: "choose"  // choose, pick, edit
  property string source: ""
  property string skillFile: ""
  property string skillDescription: ""
  property string skillNote: ""
  property string skillKey: ""
  property bool filling: false
  property bool nameFromSkill: false
  readonly property bool sourced: source !== ""
  readonly property bool skillReady: sourced && skillFile !== ""
  readonly property var skillOptions: (root.defaults.skills || []).map(function(s) { return {value: s.source, label: s.name, description: s.description} })
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
    return {name: name.text, prompt: prompt.text, source: root.source || null, model: model.text, cwd: folder.text,
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
  function setSource(path) {
    if (path === root.source) return
    root.source = path
    if (sourceField.text !== path) sourceField.text = path
    skillTimer.restart()
  }
  function readSkill() {
    skillTimer.stop()
    if (!root.sourced) {
      root.skillFile = ""; root.skillDescription = ""; root.skillNote = ""; root.skillKey = ""
      if (root.nameFromSkill) { root.fill(name, ""); root.nameFromSkill = false }
      root.fill(prompt, "")
      return
    }
    if (client.busy) { skillTimer.restart(); return }
    var params = {source: root.source}
    if (client.request("read_skill", params)) {
      root.skillKey = JSON.stringify(params)
      root.skillNote = "Reading SKILL.md..."
    }
  }
  function fill(field, text) { root.filling = true; field.text = text; root.filling = false }
  function writeOwn() { root.setSource(""); root.step = "edit"; Qt.callLater(root.focusFirst) }
  function loadSkill() { root.step = "pick"; Qt.callLater(root.focusFirst) }
  function useSkill() { if (root.skillReady) { root.step = "edit"; Qt.callLater(root.focusFirst) } }
  function back() { root.step = "choose"; root.error = ""; Qt.callLater(root.focusFirst) }
  function focusFirst() {
    if (step === "choose") writeButton.forceActiveFocus()
    else if (step === "pick") sourceField.forceActiveFocus()
    else name.forceActiveFocus()
  }
  function localTime(iso) {
    // 2026-09-14T09:00:00-04:00 -> Mon 14 Sep 09:00
    var d = new Date(iso.slice(0, 19))
    return isNaN(d) ? iso : Qt.formatDateTime(d, "ddd d MMM HH:mm")
  }

  Timer { id: previewTimer; interval: 400; onTriggered: root.preview() }
  Timer { id: skillTimer; interval: 400; onTriggered: root.readSkill() }
  Component.onCompleted: scheduleChanged()

  Connections {
    target: root.client
    function onResult(op, params, data) {
      if (!root.visible) return
      if (op === "preview_schedule" && JSON.stringify(params) === root.previewKey) {
        if (params.cron !== root.cronExpression() || params.timezone !== timezone.text) { root.scheduleChanged(); return }
        root.previewText = "Next runs · " + data.timezone + "\n" + data.occurrences.map(function(t) { return root.localTime(t.local) }).join("\n")
      } else if (op === "read_skill" && JSON.stringify(params) === root.skillKey) {
        if (params.source !== root.source) { root.readSkill(); return }
        root.skillFile = data.file
        root.skillDescription = data.description
        root.skillNote = ""
        if (name.text === "" || root.nameFromSkill) { root.fill(name, data.name); root.nameFromSkill = true }
        root.fill(prompt, data.prompt)
      } else if (op === "create_routine") root.saved(data.routine)
    }
    function onFailure(op, code, message) {
      if (!root.visible) return
      if (op === "preview_schedule") root.previewText = message
      else if (op === "read_skill") { root.skillFile = ""; root.skillDescription = ""; root.skillNote = message; root.fill(prompt, "") }
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
    Caption {
      text: root.step === "choose" ? "A routine is a prompt Claude Code runs on a schedule."
          : root.step === "pick" ? "Pick a skill folder, or type its path."
          : root.sourced ? "From " + root.skillFile : "Write your own."
    }
  }

  PanelSeparator { width: parent.width }

  // Step one: which kind of routine.
  Column {
    visible: root.step === "choose"
    width: parent.width
    spacing: Style.space(8)
    Button {
      id: writeButton
      objectName: "writeOwn"
      width: parent.width
      text: "Write your own"
      iconText: "󰏫"
      focusable: true
      bordered: true
      leftAlign: true
      onClicked: root.writeOwn()
    }
    Button {
      objectName: "loadSkill"
      width: parent.width
      text: "Load a skill folder"
      iconText: "󰉋"
      focusable: true
      bordered: true
      leftAlign: true
      onClicked: root.loadSkill()
    }
    Caption { text: "A skill folder holds a SKILL.md. Omakron reads it at every run, so editing the file changes the routine." }
  }

  // Step two: the skill folder. The field shows the chosen folder; the icon
  // opens Omarchy's folder chooser, and the skill list below fills the field.
  Column {
    visible: root.step === "pick"
    width: parent.width
    spacing: Style.space(10)
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Skill folder" }
      Row {
        width: parent.width
        spacing: Style.space(6)
        TextField {
          id: sourceField
          objectName: "field_source"
          width: parent.width - browseButton.width - parent.spacing
          placeholderText: (root.defaults.skill_roots || [])[0] || "/path/to/a/folder/with/SKILL.md"
          Accessible.name: "Skill folder path"
          onActiveFocusChanged: if (activeFocus) root.revealRequested(sourceField)
          onTextChanged: root.setSource(text)
          Keys.onReturnPressed: root.useSkill()
        }
        Button {
          id: browseButton
          objectName: "browseSkill"
          iconText: "󰉋"
          tooltipText: "Choose a folder"
          focusable: true
          bordered: true
          enabled: !chooser.running
          onClicked: chooser.running = true
        }
      }
    }
    SearchableDropdown {
      id: skillPicker
      objectName: "field_skill"
      width: parent.width
      label: "Skill"
      value: root.source
      options: root.skillOptions
      placeholderText: "Search skills"
      emptyText: "No skill folder by that name"
      onChanged: function(value) { root.setSource(value) }
    }
    Caption {
      visible: text !== ""
      text: root.skillNote !== "" ? root.skillNote : root.skillDescription
      color: root.skillNote !== "" && root.skillNote !== "Reading SKILL.md..." ? Color.urgent : root.dim
    }
  }

  // Omarchy's desktop folder chooser. It prints the chosen path, or nothing.
  Process {
    id: chooser
    command: ["omarchy-file-select", "--directory", "--title", "Skill folder"]
    stdout: StdioCollector { id: chosen; waitForEnd: true }
    onExited: function(code) {
      var path = chosen.text.trim()
      if (code === 0 && path !== "") { root.setSource(path); sourceField.forceActiveFocus() }
    }
  }

  // Step three: the routine itself.
  Column {
    visible: root.step === "edit"
    width: parent.width
    spacing: Style.space(10)
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Name" }
      TextField {
        id: name
        objectName: "field_name"
        width: parent.width
        placeholderText: "Morning repository report"
        Accessible.name: "Routine name"
        onActiveFocusChanged: if (activeFocus) root.revealRequested(name)
        onTextChanged: if (!root.filling) root.nameFromSkill = false
      }
    }

    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
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
            readOnly: root.sourced
            wrapMode: TextEdit.Wrap
            selectByMouse: true
            color: root.sourced ? root.dim : Color.foreground
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
      Caption { visible: root.sourced; text: "Read from the skill folder at every run. Edit SKILL.md to change it." }
    }
  }

  Dropdown {
    id: schedule
    visible: root.step === "edit"
    width: parent.width
    label: "Schedule"
    value: root.mode
    options: ["Daily", "Weekdays", "Weekly", "Manual", "Advanced"]
    onChanged: function(value) { root.mode = value; root.scheduleChanged() }
  }

  Row {
    visible: root.step === "edit" && root.timed
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
    visible: root.step === "edit" && root.mode === "Advanced"
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
    visible: root.step === "edit" && root.mode !== "Manual"
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

  Caption { id: previewLabel; text: root.previewText; visible: root.step === "edit" && text !== "" }

  Button {
    visible: root.step === "edit"
    text: root.showMore ? "Hide folder, model, and tools" : "Working folder, model, and tools"
    iconText: root.showMore ? "󰅃" : "󰅀"
    fontSize: Style.font.bodySmall
    focusable: true
    horizontalPadding: 0
    onClicked: root.showMore = !root.showMore
  }
  Column {
    visible: root.step === "edit" && root.showMore
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

  Caption { visible: root.step === "edit"; text: (root.defaults.policy || "Claude Code runs with its usual tools and no permission prompts.") + " New routines start paused so you can review them first." }
  Caption { text: root.error; visible: text !== ""; color: Color.urgent }

  Row {
    spacing: Style.space(8)
    Button {
      id: saveButton
      objectName: "saveRoutine"
      visible: root.step === "edit"
      text: "Save routine"
      focusable: true
      bordered: true
      enabled: root.connected && !root.client.busy
      onActiveFocusChanged: if (activeFocus) root.revealRequested(this)
      onClicked: root.save()
    }
    Button {
      objectName: "useSkill"
      visible: root.step === "pick"
      text: "Continue"
      focusable: true
      bordered: true
      enabled: root.skillReady
      onClicked: root.useSkill()
    }
    Button { objectName: "backEditor"; visible: root.step !== "choose"; text: "Back"; focusable: true; onClicked: root.back() }
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
