import QtQuick
import Quickshell
import Quickshell.Io
import "plugins/bar"
import "services"

ShellRoot {
  id: root
  property string sampleState: "ready"
  property string position: "top"
  BarWidgetRegistry { id: registry }
  Bar {
    id: bar
    omarchyPath: "/usr/share/omarchy"
    barWidgetRegistry: registry
    barConfig: ({position: root.position, layout: {left: [], center: [], right: [{id: "omarchy.agents", providers: {claude: {enabled: true}, codex: {enabled: false}, fireworks: {enabled: false}}}, {id: "omakron.routines", sampleState: root.sampleState}]}})
  }
  Component.onCompleted: {
    registry.register("omakron.routines", Qt.createComponent("file://" + Quickshell.env("OMAKRON_TEST_SHELL") + "/omakron/Widget.qml"), {})
    registry.register("omarchy.agents", Qt.createComponent("file://" + Quickshell.env("OMAKRON_TEST_SHELL") + "/plugins/agents/Panel.qml"), {})
  }
  function findItem(item, name) {
    if (!item) return null
    if (item.objectName === name) return item
    var children = item.data || item.children || item.contentItem || []
    for (var i = 0; i < children.length; ++i) {
      var found = findItem(children[i], name)
      if (found) return found
    }
    return null
  }
  function panel() {
    return findItem(bar.findPanelWidget("omakron.routines"), "routinesPanel")
  }
  IpcHandler {
    target: "test"
    function open(): void { bar.summonBarWidget("omakron.routines") }
    function state(value: string): void { root.sampleState = value }
    function position(value: string): void { root.position = value }
    function agents(): void { bar.summonBarWidget("omarchy.agents") }
    function focus(name: string): void {
      var item = findItem(root.panel(), name)
      if (item) item.forceActiveFocus()
    }
    function set(name: string, value: string): void {
      var item = findItem(root.panel(), name)
      if (item) item.text = value
    }
    function openRoutine(name: string): void {
      var p = root.panel()
      for (var i = 0; i < p.routines.length; ++i) if (p.routines[i].name === name) p.openRoutine(p.routines[i])
    }
    function editor(mode: string): void {
      var item = findItem(root.panel(), "routineEditor")
      if (item) { item.mode = mode; item.preview() }
    }
    function editorState(): string {
      var p = root.panel()
      var e = findItem(p, "routineEditor")
      return JSON.stringify({editing: p.editing, connected: p.connected, routines: p.routines, view: p.view,
        error: e ? e.error : p.error, preview: e ? e.previewText : "", mode: e ? e.mode : ""})
    }
    function inspect(): string {
      var widget = bar.findPanelWidget("omakron.routines")
      return JSON.stringify({omakron: widget ? widget.opened : null, active: bar.activePopout ? bar.activePopout.moduleName : null})
    }
  }
}
