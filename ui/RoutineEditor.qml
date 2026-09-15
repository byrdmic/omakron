import QtQuick
import QtQuick.Controls as Controls
import Quickshell.Io
import qs.Commons
import qs.Ui
import "ScheduleText.js" as ScheduleText

// Creates one routine, one decision at a time. First: write your own, or
// import a prompt from a skill file. Importing asks for the SKILL.md, chosen
// with Omarchy's file chooser or typed; once it is read, the form opens with
// the name and description filled in and the prompt tucked behind a toggle,
// and the service reads that file again at every run. The schedule is a
// segmented choice with 12-hour times, so nobody has to write cron by hand;
// the raw expression stays available under "Custom repeat". The next run
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
  property string step: "choose"  // choose, pick, edit
  property string mode: "Daily"  // Manual, Hourly, Daily, Weekly, Custom time, Custom repeat
  property int hour12: 9
  property string minute: "00"
  property string meridiem: "AM"
  property string weekday: "1"
  property var days: ["1", "2", "3", "4", "5"]
  property string permissionMode: ""
  property bool showMore: false
  property bool showPrompt: false
  property string source: ""
  property string skillFile: ""
  property string skillDescription: ""
  property string skillNote: ""
  property string skillKey: ""
  property bool filling: false
  property bool nameFromSkill: false
  readonly property bool sourced: source !== ""
  readonly property bool skillReady: sourced && skillFile !== ""
  readonly property bool manual: mode === "Manual"
  readonly property bool timed: mode === "Daily" || mode === "Weekly" || mode === "Custom time"
  readonly property color dim: Qt.darker(Color.foreground, 1.4)
  signal revealRequested(var item)
  signal reopenRequested()  // the popup closed while a chooser window had focus
  signal saved(var routine)
  signal canceled()

  onErrorChanged: if (error !== "") Qt.callLater(function() { root.revealRequested(saveButton) })

  function hour24() { return (hour12 % 12) + (meridiem === "PM" ? 12 : 0) }
  function cronExpression() {
    var at = Number(minute) + " " + hour24()
    if (manual) return null
    if (mode === "Hourly") return "0 * * * *"
    if (mode === "Daily") return at + " * * *"
    if (mode === "Weekly") return at + " * * " + weekday
    if (mode === "Custom time") return days.length === 0 ? "no days" : at + " * * " + days.slice().sort().join(",")
    return advanced.text
  }
  function draft() {
    return {name: name.text, prompt: prompt.text, source: root.source || null, model: model.text, cwd: folder.text,
      schedule_kind: manual ? "manual" : "cron", cron: cronExpression(), timezone: manual ? null : timezone.text,
      tools: tools.text, permission_mode: permissionMode || root.defaults.permission_mode || "bypassPermissions",
      mcp_config: null, env_passthrough: []}
  }
  function scheduleChanged() {
    previewText = ""
    previewTimer.restart()
  }
  function preview() {
    previewTimer.stop()
    if (manual) { previewText = "Runs only when you press Run now."; return }
    if (cronExpression() === "no days") { previewText = "Pick at least one day."; return }
    var params = {cron: cronExpression(), timezone: timezone.text}
    if (client.busy) { previewTimer.restart(); return }
    if (client.request("preview_schedule", params)) {
      previewKey = JSON.stringify(params)
      previewText = "Checking the next run times..."
    }
  }
  function save() {
    if (cronExpression() === "no days") { error = "Pick at least one day."; return }
    error = ""
    client.request("create_routine", draft())
  }
  function setSource(path) {
    if (path === root.source) return
    root.source = path
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
      root.skillNote = "Reading the skill file..."
    }
  }
  function fill(field, text) { root.filling = true; field.text = text; root.filling = false }
  function toggleDay(day) {
    var next = days.slice()
    var at = next.indexOf(day)
    if (at >= 0) next.splice(at, 1); else next.push(day)
    days = next
    scheduleChanged()
  }
  function writeOwn() { root.setSource(""); root.step = "edit"; Qt.callLater(root.focusFirst) }
  function importSkill() { root.step = "pick"; Qt.callLater(root.focusFirst) }
  function useSkill() { if (root.skillReady) { root.step = "edit"; Qt.callLater(root.focusFirst) } }
  function back() { root.step = "choose"; root.error = ""; Qt.callLater(root.focusFirst) }
  function focusFirst() {
    if (step === "choose") writeButton.forceActiveFocus()
    else if (step === "pick") sourceField.forceActiveFocus()
    else name.forceActiveFocus()
  }

  Timer { id: previewTimer; interval: 400; onTriggered: root.preview() }
  Timer { id: skillTimer; interval: 400; onTriggered: root.readSkill() }
  Component.onCompleted: scheduleChanged()

  Connections {
    target: root.client
    // Replies are handled even while the popup is closed: a chooser window
    // takes focus and closes it, and the editor state outlives that.
    function onResult(op, params, data) {
      if (op === "preview_schedule" && JSON.stringify(params) === root.previewKey) {
        if (params.cron !== root.cronExpression() || params.timezone !== timezone.text) { root.scheduleChanged(); return }
        root.previewText = "Next runs · " + data.timezone + "\n" + data.occurrences.map(function(t) { return ScheduleText.localMoment(t.local) }).join("\n")
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
      if (op === "preview_schedule") root.previewText = message
      else if (op === "read_skill") { root.skillFile = ""; root.skillDescription = ""; root.skillNote = message; root.fill(prompt, "") }
      else root.error = message
    }
  }

  // Omarchy's desktop file chooser. It prints the chosen path, or nothing.
  Process {
    id: chooser
    command: ["omarchy-file-select", "--title", "Skill file", "--extensions", "md"]
    stdout: StdioCollector { id: chosen; waitForEnd: true }
    onExited: function(code) {
      var path = chosen.text.trim()
      if (code === 0 && path !== "") sourceField.text = path
      root.reopenRequested()
      Qt.callLater(function() { sourceField.forceActiveFocus() })
    }
  }

  // Omarchy's desktop folder chooser for the working folder.
  Process {
    id: folderChooser
    command: ["omarchy-file-select", "--directory", "--title", "Working folder"]
    stdout: StdioCollector { id: chosenFolder; waitForEnd: true }
    onExited: function(code) {
      var path = chosenFolder.text.trim()
      if (code === 0 && path !== "") folder.text = path
      root.reopenRequested()
      Qt.callLater(function() { folder.forceActiveFocus() })
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
          : root.step === "pick" ? "Choose the SKILL.md to import the prompt from."
          : root.sourced ? "Imported from " + root.skillFile : "Write your own."
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
      objectName: "importSkill"
      width: parent.width
      text: "Import a prompt from a skill file"
      iconText: "󰈔"
      focusable: true
      bordered: true
      leftAlign: true
      onClicked: root.importSkill()
    }
    Caption { text: "A skill file is a SKILL.md. Omakron reads it at every run, so editing the file changes the routine." }
  }

  // Step two: the skill file. The icon opens Omarchy's file chooser; the path can also be typed.
  Column {
    visible: root.step === "pick"
    width: parent.width
    spacing: Style.space(10)
    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Skill file" }
      Row {
        width: parent.width
        spacing: Style.space(6)
        TextField {
          id: sourceField
          objectName: "field_source"
          width: parent.width - browseButton.width - parent.spacing
          placeholderText: "/path/to/skill/SKILL.md"
          Accessible.name: "Skill file path"
          onActiveFocusChanged: if (activeFocus) root.revealRequested(sourceField)
          onTextChanged: root.setSource(text.trim())
          Keys.onReturnPressed: root.useSkill()
        }
        Button {
          id: browseButton
          objectName: "browseSkill"
          iconText: "󰈔"
          tooltipText: "Choose a file"
          focusable: true
          bordered: true
          enabled: !chooser.running
          onClicked: chooser.running = true
        }
      }
    }
    Column {
      visible: root.skillReady || root.skillNote !== ""
      width: parent.width
      spacing: Style.space(2)
      Text {
        visible: root.skillReady
        width: parent.width
        text: name.text
        elide: Text.ElideRight
        textFormat: Text.PlainText
        color: Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
      Caption {
        visible: text !== ""
        text: root.skillNote !== "" ? root.skillNote : root.skillDescription
        color: root.skillNote !== "" && root.skillNote !== "Reading the skill file..." ? Color.urgent : root.dim
      }
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
      Caption { visible: root.sourced && root.skillDescription !== ""; text: root.skillDescription }
    }

    Button {
      visible: root.sourced
      objectName: "togglePrompt"
      text: root.showPrompt ? "Hide the prompt" : "Show the prompt"
      iconText: root.showPrompt ? "󰅃" : "󰅀"
      fontSize: Style.font.bodySmall
      focusable: true
      horizontalPadding: 0
      onClicked: root.showPrompt = !root.showPrompt
    }
    Column {
      visible: !root.sourced || root.showPrompt
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { visible: !root.sourced; text: "Prompt" }
      BorderSurface {
        id: promptFrame
        width: parent.width
        height: Style.space(root.sourced ? 180 : 120)
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
      Caption { visible: root.sourced; text: "Read from the skill file at every run. Edit the file to change it." }
    }

    Column {
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Schedule" }
      ButtonGroup {
        id: schedule
        objectName: "field_schedule"
        width: parent.width
        value: root.mode
        options: ["Manual", "Hourly", "Daily", "Weekly", "Custom time", "Custom repeat"]
        onChanged: function(value) { root.mode = value; root.scheduleChanged() }
      }
    }

    Caption { visible: root.mode === "Hourly"; text: "Every hour, on the hour." }

    Dropdown {
      id: weekdayPicker
      visible: root.mode === "Weekly"
      width: parent.width
      label: "Day"
      value: root.weekday
      options: [{value:"1",label:"Monday"},{value:"2",label:"Tuesday"},{value:"3",label:"Wednesday"},{value:"4",label:"Thursday"},{value:"5",label:"Friday"},{value:"6",label:"Saturday"},{value:"0",label:"Sunday"}]
      onChanged: function(value) { root.weekday = value; root.scheduleChanged() }
    }

    Column {
      visible: root.mode === "Custom time"
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Days" }
      Row {
        spacing: Style.space(4)
        Repeater {
          model: [{value:"1",label:"Mon"},{value:"2",label:"Tue"},{value:"3",label:"Wed"},{value:"4",label:"Thu"},{value:"5",label:"Fri"},{value:"6",label:"Sat"},{value:"0",label:"Sun"}]
          Button {
            required property var modelData
            text: modelData.label
            fontSize: Style.font.bodySmall
            focusable: true
            bordered: true
            selected: root.days.indexOf(modelData.value) >= 0
            onClicked: root.toggleDay(modelData.value)
          }
        }
      }
    }

    Column {
      visible: root.timed
      width: parent.width
      spacing: Style.spacing.labelGap
      FieldLabel { text: "Time" }
      Row {
        spacing: Style.space(6)
        Dropdown {
          id: hourPicker
          objectName: "field_hour"
          width: Style.space(72)
          showLabel: false
          value: String(root.hour12)
          options: ["12", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"]
          onChanged: function(value) { root.hour12 = Number(value); root.scheduleChanged() }
        }
        Dropdown {
          id: minutePicker
          objectName: "field_minute"
          width: Style.space(72)
          showLabel: false
          value: root.minute
          options: [{value:"00",label:":00"},{value:"15",label:":15"},{value:"30",label:":30"},{value:"45",label:":45"}]
          onChanged: function(value) { root.minute = value; root.scheduleChanged() }
        }
        ButtonGroup {
          id: meridiemPicker
          objectName: "field_meridiem"
          width: Style.space(110)
          value: root.meridiem
          options: ["AM", "PM"]
          onChanged: function(value) { root.meridiem = value; root.scheduleChanged() }
        }
      }
    }

    Column {
      visible: root.mode === "Custom repeat"
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
      Caption { text: "Five fields: minute, hour, day of month, month, day of week. 0 9 * * 1-5 is weekdays at 9:00 AM." }
    }

    Caption { id: previewLabel; text: root.previewText; visible: text !== "" }

    Button {
      text: root.showMore ? "Hide time zone, folder, model, and tools" : "Time zone, folder, model, and tools"
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
        Caption { text: "Times above are in this zone. It starts as this machine's zone." }
      }
      Column {
        width: parent.width
        spacing: Style.spacing.labelGap
        FieldLabel { text: "Working folder" }
        Row {
          width: parent.width
          spacing: Style.space(6)
          TextField {
            id: folder
            objectName: "field_folder"
            width: parent.width - folderButton.width - parent.spacing
            text: root.defaults.cwd || ""
            Accessible.name: "Working folder"
            onActiveFocusChanged: if (activeFocus) root.revealRequested(folder)
          }
          Button {
            id: folderButton
            objectName: "browseFolder"
            iconText: "󰉋"
            tooltipText: "Choose a folder"
            focusable: true
            bordered: true
            enabled: !folderChooser.running
            onClicked: folderChooser.running = true
          }
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
