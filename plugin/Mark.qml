import QtQuick
import qs.Commons

// Two neighboring spaces. Colors follow the active Omarchy theme.
Item {
  implicitWidth: 48
  implicitHeight: 48
  Rectangle {
    x: parent.width * 6 / 64; y: parent.height * 18 / 64
    width: parent.width * 28 / 64; height: parent.height * 38 / 64
    radius: 2
    color: "transparent"
    border.color: Color.foreground
    border.width: Math.max(1, parent.width * 3 / 64)
  }
  Rectangle {
    x: parent.width * 42 / 64; y: parent.height * 8 / 64
    width: parent.width * 16 / 64; height: parent.height * 48 / 64
    radius: 2
    color: Color.accent
  }
}
