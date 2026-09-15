import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root
  objectName: "serviceClient"
  visible: false
  // One request at a time, from the call to request() until its reply is
  // delivered. Process.running turns true only once the child has started,
  // so two requests in one tick would both be accepted and the second start
  // would find stdin closed and wait forever. This flag closes that gap.
  property bool busy: false
  property string operation: ""
  property var parameters: ({})
  signal result(string operation, var parameters, var data)
  signal failure(string operation, string code, string message)

  function request(op, params) {
    if (busy) return false
    busy = true
    operation = op
    parameters = params || {}
    process.stdinEnabled = true
    process.running = true
    return true
  }
  Process {
    id: process
    command: ["python3", Qt.resolvedUrl("../scripts/client.py").toString().replace(/^file:\/\//, ""), "request"]
    onStarted: {
      write(JSON.stringify({op: root.operation, params: root.parameters}))
      stdinEnabled = false
    }
    stdout: StdioCollector { id: output; waitForEnd: true }
    stderr: StdioCollector { id: diagnostic; waitForEnd: true }
    onExited: function(code) {
      var op = root.operation
      var params = root.parameters
      var text = output.text
      var details = diagnostic.text
      // Process finishes its state transition after emitting exited.
      // Deliver the response on the next event turn so a follow-up can start.
      Qt.callLater(function() {
        root.busy = false
        try {
          var data = JSON.parse(text)
          if (code === 0) root.result(op, params, data)
          else root.failure(op, data.error ? data.error.code : "client", data.error ? data.error.message : details)
        } catch (e) {
          root.failure(op, "unreachable", "The service did not respond. Start omakron.service, then reconnect.")
        }
      })
    }
  }
}
