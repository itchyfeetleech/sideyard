import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui as Ui

Item {
  id: root
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""
  property bool opened: false
  property var sessions: []
  property string deleteTarget: ""
  property string error: action.error || poll.error

  function open(payload) {
    opened = true
    poll.run(["list"])
    Qt.callLater(function() { nameField.forceActiveFocus() })
  }
  function close() {
    opened = false
    deleteTarget = ""
  }
  function dismiss() {
    close()
    if (shell) shell.hide("hoppcx.agent-workspaces")
  }
  function create() {
    if (nameField.acceptableInput && nameField.text && !action.busy)
      action.run(["start", nameField.text])
  }
  Command {
    id: poll
    onFinished: function(result) { if (result.sessions) root.sessions = result.sessions }
  }
  Command {
    id: action
    onFinished: function(result) {
      poll.run(["list"])
      if (result.deleted) root.deleteTarget = ""
      if (!result.error && (result.host_workspace || result.viewer_pid)) root.dismiss()
    }
  }
  Timer {
    interval: 2000
    repeat: true
    running: root.opened && !action.busy
    onTriggered: poll.run(["list"])
  }
  PanelWindow {
    id: window
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "agent-workspaces"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    Rectangle { anchors.fill: parent; color: Color.menu.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }
    Ui.BorderSurface {
      anchors.centerIn: parent
      width: Math.min(Style.space(660), window.width - Style.space(40))
      height: Math.min(content.implicitHeight + Style.space(40), window.height - Style.space(60))
      color: Color.menu.background
      radius: Style.cornerRadius
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      MouseArea { anchors.fill: parent; onClicked: {} }
      Flickable {
        anchors.fill: parent
        anchors.margins: Style.space(20)
        contentHeight: content.implicitHeight
        clip: true
        ColumnLayout {
          id: content
          width: parent.width
          spacing: Style.space(12)
          Keys.onEscapePressed: { if (root.deleteTarget) root.deleteTarget = ""; else root.dismiss() }
          RowLayout {
            Layout.fillWidth: true
            Mark { Layout.preferredWidth: Style.space(44); Layout.preferredHeight: Style.space(44) }
            Text {
              text: "Sideyard"
              color: Color.foreground
              font { family: Style.font.family; pixelSize: Style.space(30); bold: true }
              Layout.fillWidth: true
            }
            Ui.Button { text: "Close"; focusable: true; onClicked: root.dismiss() }
          }
          Text {
            text: "A desktop for your agent. Yours stays yours."
            color: Color.foreground
            opacity: 0.7
            font { family: Style.font.family; pixelSize: Style.font.body }
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
          }
          RowLayout {
            Layout.fillWidth: true
            Ui.TextField {
              id: nameField
              placeholderText: "Workspace name"
              validator: RegularExpressionValidator { regularExpression: /[A-Za-z0-9][A-Za-z0-9_-]{0,63}/ }
              Layout.fillWidth: true
              enabled: !action.busy
              onAccepted: root.create()
              Keys.onEscapePressed: { if (root.deleteTarget) root.deleteTarget = ""; else root.dismiss() }
            }
            Ui.Button {
              text: action.busy ? "Working…" : "Create workspace"
              focusable: true
              enabled: !action.busy && nameField.acceptableInput && nameField.text.length > 0
              onClicked: root.create()
            }
          }
          Repeater {
            model: root.sessions
            delegate: ColumnLayout {
              id: card
              required property var modelData
              Layout.fillWidth: true
              spacing: Style.space(4)
              RowLayout {
                Layout.fillWidth: true
                Text {
                  text: (card.modelData.live && card.modelData.host_workspace ? card.modelData.host_workspace + "  ·  " : "") + card.modelData.session_id
                  elide: Text.ElideRight
                  color: Color.foreground
                  font { family: Style.font.family; pixelSize: Style.font.body; bold: true }
                  Layout.fillWidth: true
                }
                Text {
                  text: !card.modelData.live ? "Stopped" : card.modelData.owner === "human" ? "You have control" : card.modelData.owner === "paused" ? "Paused" : "Agent control"
                  color: Color.foreground
                  opacity: 0.7
                  font { family: Style.font.family; pixelSize: Style.font.body }
                }
              }
              RowLayout {
                Layout.fillWidth: true
                enabled: !action.busy
                Ui.Button {
                  text: card.modelData.live ? "Open" : "Start"
                  focusable: true
                  onClicked: action.run([card.modelData.live ? "open" : "start", card.modelData.session_id])
                }
                Ui.Button {
                  visible: card.modelData.live
                  text: card.modelData.owner === "agent" ? "Take control" : "Return to agent"
                  focusable: true
                  onClicked: action.run(["control", card.modelData.session_id, card.modelData.owner === "agent" ? "human" : "agent"])
                }
                Ui.Button {
                  visible: card.modelData.live && card.modelData.owner === "agent"
                  text: "Pause"
                  focusable: true
                  onClicked: action.run(["control", card.modelData.session_id, "paused"])
                }
                Ui.Button {
                  visible: card.modelData.live
                  text: "Stop"
                  tooltipText: "Closes this desktop and its apps; workspace files are kept"
                  focusable: true
                  onClicked: action.run(["stop", card.modelData.session_id])
                }
                Item { Layout.fillWidth: true }
                Ui.Button {
                  text: "Delete…"
                  tooltipText: "Remove this workspace and its app profiles"
                  focusable: true
                  onClicked: root.deleteTarget = card.modelData.session_id
                }
              }
              Rectangle {
                visible: root.deleteTarget === card.modelData.session_id
                Layout.fillWidth: true
                implicitHeight: deletion.implicitHeight + Style.space(24)
                color: Qt.alpha(Color.foreground, 0.04)
                radius: Style.cornerRadius
                ColumnLayout {
                  id: deletion
                  anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(12) }
                  spacing: Style.space(10)
                  Text {
                    text: "Delete “" + card.modelData.session_id + "”? This closes its apps and permanently removes workspace files and profiles. Attached projects are kept."
                    color: Color.foreground
                    textFormat: Text.PlainText
                    font { family: Style.font.family; pixelSize: Style.font.body }
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                  }
                  RowLayout {
                    Layout.alignment: Qt.AlignRight
                    Ui.Button {
                      text: "Cancel"
                      focusable: true
                      enabled: !action.busy
                      onClicked: root.deleteTarget = ""
                    }
                    Ui.Button {
                      text: "Delete permanently"
                      accent: Color.urgent
                      focusable: true
                      enabled: !action.busy
                      onClicked: action.run(["delete", card.modelData.session_id, "--yes"])
                    }
                  }
                }
              }
              Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: Style.space(8)
                Layout.bottomMargin: Style.space(8)
                implicitHeight: 1
                color: Qt.alpha(Color.foreground, 0.12)
              }
            }
          }
          Text {
            visible: root.sessions.length === 0
            text: "Your next agent desktop starts here. Connect any MCP agent with sideyard mcp."
            color: Color.foreground
            font { family: Style.font.family; pixelSize: Style.font.body }
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
          }
          Text {
            visible: root.error.length > 0
            text: root.error
            color: Color.foreground
            font { family: Style.font.family; pixelSize: Style.font.body }
            wrapMode: Text.WrapAnywhere
            Layout.fillWidth: true
          }
        }
      }
    }
  }
}
