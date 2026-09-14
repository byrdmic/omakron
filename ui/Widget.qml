import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "omakron.routines"
  readonly property bool opened: panel.opened
  readonly property bool popoutSwitchClosing: panel.popoutSwitchClosing
  function open() { panel.open() }
  function close() { panel.close(); button.forceActiveFocus() }
  function closeForPopoutSwitch() { panel.closeForPopoutSwitch() }
  function togglePanel() { opened ? close() : open() }
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  RoutinesPanel {
    id: panel
    bar: root.bar
    settings: root.settings
    anchorItem: button
    hostWidget: root
  }
  IpcHandler {
    target: "omakron.routines"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.togglePanel() }
    function state(): string { return JSON.stringify({opened: root.opened, focus: button.focus, editing: panel.editing}) }
  }
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    tooltipText: "Routines"
    activeFocusOnTab: true
    Accessible.name: "Routines"
    Accessible.role: Accessible.Button
    Keys.onReturnPressed: root.togglePanel()
    Keys.onSpacePressed: root.togglePanel()
    onPressed: root.togglePanel()
  }
}
