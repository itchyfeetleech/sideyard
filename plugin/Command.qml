import QtQuick
import Quickshell
import Quickshell.Io

// Run the same short-lived CLI used by agents. No manager service to install.
Item {
  id: root
  property bool busy: process.running
  property string error: ""
  signal finished(var result)
  function run(args) {
    if (busy) return
    error = ""
    process.command = [Quickshell.env("HOME") + "/.local/bin/sideyard"].concat(args)
    process.running = true
  }
  Process {
    id: process
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var result = JSON.parse(text)
          if (result.error) root.error = result.error.message || "Action failed"
          root.finished(result)
        } catch (e) { root.error = "Could not read workspace status" }
      }
    }
    stderr: StdioCollector {
      onStreamFinished: { if (text.trim()) root.error = text.trim() }
    }
    onExited: function(code, status) {
      if (code !== 0 && !root.error) root.error = "Workspace command failed (" + code + ")"
    }
  }
}
