import QtQuick
import Quickshell.Io

Item {
  id: root

  property bool active: false
  property string path: ""
  readonly property bool connected: owns(currentSocket) && currentSocket.connected

  signal opened()
  signal eventReceived(string name)

  property bool initialized: false
  property var currentSocket: null

  function owns(socket) {
    return initialized && active && socket !== null && currentSocket === socket
      && path !== "" && socket.path === path
  }

  function closeSocket() {
    var socket = currentSocket
    // Revoke ownership before disconnecting: native signals can fire synchronously.
    currentSocket = null
    if (socket) {
      socket.connected = false
      socket.destroy()
    }
  }

  function connectSocket() {
    if (!initialized || !active || path === "" || currentSocket) return
    // A failed initial connect leaves the native Socket holding its QLocalSocket.
    // Recreate each attempt, also giving it a fresh newline-framing buffer.
    currentSocket = socketComponent.createObject(root, { path: path })
    if (currentSocket) currentSocket.connected = true
    else reconnectTimer.restart()
  }

  function reconnect(socket) {
    if (!owns(socket)) return
    closeSocket()
    if (initialized && active && path !== "") reconnectTimer.restart()
  }

  function reset() {
    if (!initialized) return
    reconnectTimer.stop()
    closeSocket()
    connectSocket()
  }

  onActiveChanged: reset()
  onPathChanged: reset()
  Component.onCompleted: {
    initialized = true
    reset()
  }
  Component.onDestruction: {
    initialized = false
    reconnectTimer.stop()
    closeSocket()
  }

  Timer {
    id: reconnectTimer
    interval: 750
    onTriggered: root.connectSocket()
  }

  Component {
    id: socketComponent

    Socket {
      id: socket

      onConnectionStateChanged: {
        if (!root.owns(socket)) return
        if (connected) root.opened()
        else root.reconnect(socket)
      }
      onError: root.reconnect(socket)

      parser: SplitParser {
        splitMarker: "\n"
        onRead: data => {
          if (!root.owns(socket) || !socket.connected) return
          var separator = data.indexOf(">>")
          if (separator > 0) root.eventReceived(data.slice(0, separator))
        }
      }
    }
  }
}
