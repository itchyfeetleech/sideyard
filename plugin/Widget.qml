import QtQuick
import Quickshell.Hyprland
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "hoppcx.agent-workspaces"
  implicitWidth: row.implicitWidth
  implicitHeight: barSize
  property var sessions: []
  readonly property var activeSession: {
    var id = Hyprland.focusedWorkspace ? Hyprland.focusedWorkspace.id : -1
    return sessions.find(function(s) { return s.live && s.viewer_open && s.host_workspace === id }) || null
  }
  Command {
    id: poll
    onFinished: function(result) { if (result.sessions) root.sessions = result.sessions }
  }
  Command {
    id: action
    onFinished: function(result) { poll.run(["list"]) }
  }
  Timer {
    interval: 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: poll.run(["list"])
  }
  Row {
    id: row
    WidgetButton {
      bar: root.bar
      fixedHeight: root.barSize
      text: root.activeSession ? "AGENT · " + root.activeSession.session_id + " · " + (root.activeSession.owner === "human" ? "You have control" : root.activeSession.owner === "paused" ? "Paused" : "Watching") : "Sideyard · " + root.sessions.filter(function(s) { return s.live }).length
      foreground: root.activeSession ? Color.accent : (root.bar ? root.bar.barForeground : Color.foreground)
      tooltipText: action.error || "Sideyard — agent desktops"
      onPressed: if (root.bar && root.bar.shell) root.bar.shell.toggle(root.moduleName, "{}")
    }
    WidgetButton {
      bar: root.bar
      fixedHeight: root.barSize
      visible: root.activeSession !== null
      text: root.activeSession && root.activeSession.owner === "human" ? "Watch / give to agent" : "Take control"
      interactive: !action.busy
      onPressed: action.run(["control", root.activeSession.session_id, root.activeSession.owner === "human" ? "agent" : "human"])
    }
    WidgetButton {
      bar: root.bar
      fixedHeight: root.barSize
      visible: root.activeSession !== null
      text: "Stop"
      interactive: !action.busy
      onPressed: action.run(["stop", root.activeSession.session_id])
    }
  }
}
