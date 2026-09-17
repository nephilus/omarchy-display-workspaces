import QtQuick
import qs.Commons

Item {
  id: root

  property var displays: []
  property var positions: []
  property bool editable: true
  property string selectedName: ""
  signal selected(string name)
  signal positionEdited(string name, int x, int y)

  implicitWidth: Style.space(600)
  implicitHeight: Style.space(180)
  clip: true

  property string pressedName: ""
  property bool dragMoved: false
  property real pressX: 0
  property real pressY: 0
  property real startX: 0
  property real startY: 0
  property real previewX: 0
  property real previewY: 0
  property real movingWidth: 1
  property real movingHeight: 1
  property var frozenView: ({ scale: 1, x: 0, y: 0 })
  readonly property bool dragging: pressedName !== "" && dragMoved
  readonly property real inset: Style.space(10)
  readonly property real legendHeight: overlaps.length ? Style.space(22) : 0
  readonly property var rectangles: buildRectangles()
  readonly property var fittedView: fitView()
  readonly property var view: pressedName !== "" ? frozenView : fittedView
  readonly property var overlaps: findOverlaps()

  function finiteOr(value, fallback) {
    return typeof value === "number" && isFinite(value) ? value : fallback
  }

  function buildRectangles() {
    var result = []
    for (var i = 0; i < displays.length; ++i) {
      var monitor = displays[i]
      var point = monitor
      for (var j = 0; j < positions.length; ++j) {
        if (positions[j].name === monitor.name) {
          point = positions[j]
          break
        }
      }
      result.push({
        name: monitor.name,
        x: finiteOr(point.x, 0),
        y: finiteOr(point.y, 0),
        width: Math.max(1, finiteOr(monitor.logicalWidth, 1)),
        height: Math.max(1, finiteOr(monitor.logicalHeight, 1))
      })
    }
    return result
  }

  function fitView() {
    if (!rectangles.length) return { scale: 1, x: width / 2, y: height / 2 }
    var left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity
    for (var i = 0; i < rectangles.length; ++i) {
      var rect = rectangles[i]
      left = Math.min(left, rect.x)
      top = Math.min(top, rect.y)
      right = Math.max(right, rect.x + rect.width)
      bottom = Math.max(bottom, rect.y + rect.height)
    }
    var availableWidth = Math.max(1, width - inset * 2)
    var availableHeight = Math.max(1, height - legendHeight - inset * 2)
    var scale = Math.min(availableWidth / (right - left), availableHeight / (bottom - top)) * 0.88
    return {
      scale: scale,
      x: width / 2 - (left + right) / 2 * scale,
      y: (height - legendHeight) / 2 - (top + bottom) / 2 * scale
    }
  }

  function shownX(rect) { return rect.name === pressedName ? previewX : rect.x }
  function shownY(rect) { return rect.name === pressedName ? previewY : rect.y }

  function findOverlaps() {
    var names = []
    for (var i = 0; i < rectangles.length; ++i) {
      var a = rectangles[i]
      var ax = shownX(a), ay = shownY(a)
      for (var j = i + 1; j < rectangles.length; ++j) {
        var b = rectangles[j]
        var bx = shownX(b), by = shownY(b)
        if (ax < bx + b.width && ax + a.width > bx && ay < by + b.height && ay + a.height > by) {
          if (names.indexOf(a.name) < 0) names.push(a.name)
          if (names.indexOf(b.name) < 0) names.push(b.name)
        }
      }
    }
    return names
  }

  function startDrag(rect, point) {
    // Freeze the transform before moving the preview, so the map cannot chase the pointer.
    frozenView = { scale: fittedView.scale, x: fittedView.x, y: fittedView.y }
    pressX = point.x
    pressY = point.y
    startX = rect.x
    startY = rect.y
    previewX = rect.x
    previewY = rect.y
    movingWidth = rect.width
    movingHeight = rect.height
    dragMoved = false
    pressedName = rect.name
  }

  function moveDrag(point) {
    if (!editable || !pressedName) return
    var dx = point.x - pressX, dy = point.y - pressY
    if (!dragMoved && Math.sqrt(dx * dx + dy * dy) < Style.space(4)) return
    dragMoved = true
    var x = startX + dx / frozenView.scale
    var y = startY + dy / frozenView.scale
    var snapX = x, snapY = y
    var distanceX = Style.space(9) / frozenView.scale
    var distanceY = distanceX
    for (var i = 0; i < rectangles.length; ++i) {
      var other = rectangles[i]
      if (other.name === pressedName) continue
      // Adjacent edges, matching edges, then centre alignment, in tie-break order.
      var xs = [other.x + other.width, other.x - movingWidth, other.x,
                other.x + other.width - movingWidth, other.x + (other.width - movingWidth) / 2]
      var ys = [other.y + other.height, other.y - movingHeight, other.y,
                other.y + other.height - movingHeight, other.y + (other.height - movingHeight) / 2]
      for (var j = 0; j < xs.length; ++j) {
        var nextX = Math.abs(xs[j] - x)
        if (nextX < distanceX) { distanceX = nextX; snapX = xs[j] }
        var nextY = Math.abs(ys[j] - y)
        if (nextY < distanceY) { distanceY = nextY; snapY = ys[j] }
      }
    }
    previewX = Math.round(snapX)
    previewY = Math.round(snapY)
  }

  function cancelDrag() {
    pressedName = ""
    dragMoved = false
  }

  function finishDrag() {
    var name = pressedName
    var x = Math.round(previewX), y = Math.round(previewY)
    var changed = editable && dragMoved && (x !== startX || y !== startY)
    cancelDrag()
    if (name && changed) positionEdited(name, x, y)
  }

  // Incoming snapshots or numeric edits must never commit an old pointer gesture.
  onDisplaysChanged: cancelDrag()
  onPositionsChanged: cancelDrag()
  onEditableChanged: if (!editable) cancelDrag()
  onVisibleChanged: if (!visible) cancelDrag()

  Rectangle {
    anchors.fill: parent
    radius: Style.space(6)
    color: "transparent"
    border.color: Color.menu.selectedBackground
    border.width: 1
  }

  Item {
    id: mapArea
    width: parent.width
    height: Math.max(0, parent.height - root.legendHeight)
    clip: true

    Repeater {
      model: root.displays
      delegate: Rectangle {
        id: monitorBox
        required property var modelData
        required property int index
        readonly property var rect: root.rectangles[index] || ({ name: "", x: 0, y: 0, width: 1, height: 1 })
        readonly property bool chosen: root.selectedName === modelData.name
        readonly property bool overlapping: root.overlaps.indexOf(modelData.name) >= 0
        readonly property bool moving: root.pressedName === modelData.name

        x: root.view.x + root.shownX(rect) * root.view.scale
        y: root.view.y + root.shownY(rect) * root.view.scale
        width: rect.width * root.view.scale
        height: rect.height * root.view.scale
        z: moving ? 3 : chosen ? 2 : 1
        radius: Style.space(4)
        color: Color.menu.selectedBackground
        border.color: overlapping ? Color.urgent : chosen ? Color.accent : Color.foreground
        border.width: chosen || overlapping ? 2 : 1
        clip: true

        Rectangle {
          anchors.fill: parent
          anchors.margins: Style.space(4)
          visible: monitorBox.chosen && monitorBox.overlapping && parent.width > Style.space(12) && parent.height > Style.space(12)
          radius: Style.space(2)
          color: "transparent"
          border.color: Color.accent
          border.width: 1
        }

        Column {
          anchors.centerIn: parent
          width: Math.max(0, parent.width - Style.space(14))
          spacing: Style.space(3)
          Text {
            width: parent.width
            text: (monitorBox.modelData.icon || "\uf108") + " " + (monitorBox.modelData.label || monitorBox.modelData.name)
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: monitorBox.chosen
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
          Text {
            width: parent.width
            visible: monitorBox.height > Style.space(62)
            text: monitorBox.modelData.name + " · " + Math.round(monitorBox.rect.width) + " × " + Math.round(monitorBox.rect.height)
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
          Text {
            width: parent.width
            visible: monitorBox.height > Style.space(92)
            text: Math.round(root.shownX(monitorBox.rect)) + ", " + Math.round(root.shownY(monitorBox.rect))
            color: monitorBox.overlapping ? Color.urgent : Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
        }

        MouseArea {
          id: pointer
          objectName: "layout-" + monitorBox.modelData.name
          anchors.fill: parent
          acceptedButtons: Qt.LeftButton
          preventStealing: true
          cursorShape: !root.editable ? Qt.PointingHandCursor : monitorBox.moving && root.dragMoved ? Qt.ClosedHandCursor : Qt.OpenHandCursor
          onPressed: function(mouse) {
            forceActiveFocus()
            root.selectedName = monitorBox.modelData.name
            root.selected(monitorBox.modelData.name)
            if (root.editable) root.startDrag(monitorBox.rect, mapToItem(root, mouse.x, mouse.y))
          }
          onPositionChanged: function(mouse) {
            if (pressed && monitorBox.moving) root.moveDrag(mapToItem(root, mouse.x, mouse.y))
          }
          onReleased: if (monitorBox.moving) root.finishDrag()
          onCanceled: if (monitorBox.moving) root.cancelDrag()
          Keys.onEscapePressed: function(event) { root.cancelDrag(); event.accepted = true }
        }
      }
    }

    Text {
      anchors.centerIn: parent
      visible: !root.displays.length
      width: Math.max(0, parent.width - root.inset * 2)
      text: "No usable displays"
      color: Color.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignHCenter
      elide: Text.ElideRight
    }
  }

  Text {
    x: root.inset
    y: root.height - root.legendHeight
    width: Math.max(0, root.width - root.inset * 2)
    height: root.legendHeight
    visible: root.overlaps.length > 0
    text: "Displays overlap"
    color: Color.urgent
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
}
