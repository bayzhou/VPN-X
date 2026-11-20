"""Paint interface icons and render the application logo at Windows icon sizes."""
from pathlib import Path
from struct import pack, unpack_from

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QRectF, Qt
from PySide6.QtGui import QIcon, QIconEngine, QImage, QPainter, QPixmap, qAlpha
from PySide6.QtSvg import QSvgRenderer


APP_ICON_SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def application_icon_frames(path):
    source = QImage(str(path))
    left, top, right, bottom = source.width(), source.height(), -1, -1
    for y in range(source.height()):
        for x in range(source.width()):
            if qAlpha(source.pixel(x, y)):
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    if right < 0:
        raise ValueError(f"Missing or empty application icon: {path}")
    logo = source.copy(QRect(left, top, right - left + 1, bottom - top + 1))

    for size in APP_ICON_SIZES:
        inset = 0 if size == 16 else round(size * 0.04)
        edge = size - 2 * inset
        scaled = logo.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        frame = QImage(size, size, QImage.Format.Format_ARGB32)
        frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(frame)
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        painter.end()
        yield frame


def application_icon(path):
    icon = QIcon()
    for frame in application_icon_frames(path):
        icon.addPixmap(QPixmap.fromImage(frame))
    return icon


def write_application_icon(source, output):
    # Qt writes one image per ICO. Assemble their directory records into a multi-size resource.
    directory = bytearray(pack("<HHH", 0, 1, len(APP_ICON_SIZES)))
    images = bytearray()
    first_offset = len(directory) + 16 * len(APP_ICON_SIZES)
    for frame in application_icon_frames(source):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not frame.save(buffer, "ICO"):
            raise OSError("Could not render the Windows application icon")
        buffer.close()
        ico = bytes(data)
        length, offset = unpack_from("<II", ico, 14)
        directory.extend(ico[6:18])
        directory.extend(pack("<I", first_offset + len(images)))
        images.extend(ico[offset:offset + length])
    Path(output).write_bytes(directory + images)


PATHS = {
    "search": '<circle cx="10.8" cy="10.8" r="5.8"/><path d="m15 15 4.2 4.2"/>',
    "proxy": '<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M6 12h3l2-4 2.4 8 2-4H18"/>',
    "settings": '<path d="M4 6h3m4 0h9M4 12h9m4 0h3M4 18h5m4 0h7"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="11" cy="18" r="2"/>',
    "advanced": '<path d="M7.5 10V8a4.5 4.5 0 0 1 9 0v2m-4.5 4v2.5"/><rect x="5" y="10" width="14" height="10" rx="2.5"/>',
    "logs": '<rect x="5" y="3" width="14" height="18" rx="2.5"/><path d="M9 8h6m-6 4h6m-6 4h4"/>',
    "minimize": '<path d="M6.5 12h11"/>',
    "fullScreen": '<path d="M9 5.5H5.5V9m9-3.5H18V9M5.5 15v3.5H9m9-3.5v3.5h-3.5"/>',
    "exitFullScreen": '<path d="M5.5 9H9V5.5m9 3.5h-3.5V5.5M9 18.5V15H5.5m9 3.5V15H18"/>',
    "close": '<path d="m7 7 10 10m0-10L7 17"/>',
}


class StrokeIcon(QIconEngine):
    def __init__(self, name, inverse=False):
        super().__init__()
        self.name = name
        self.inverse = inverse

    def clone(self):
        return StrokeIcon(self.name, self.inverse)

    def paint(self, painter, rect, mode, state):
        ink = "#767676" if mode == QIcon.Mode.Disabled else ("#ffffff" if self.inverse else "#262626")
        source = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                  f'<g fill="none" stroke="{ink}" stroke-width="1.8" stroke-linecap="round" '
                  f'stroke-linejoin="round">{PATHS[self.name]}</g></svg>')
        QSvgRenderer(QByteArray(source.encode("ascii"))).render(painter, QRectF(rect))

    def pixmap(self, size, mode, state):
        pix = QPixmap(size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        self.paint(painter, pix.rect(), mode, state)
        painter.end()
        return pix


def stroke_icon(name, *, inverse=False):
    return QIcon(StrokeIcon(name, inverse))
