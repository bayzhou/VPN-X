"""Application title bar and Windows window decorations."""
from ctypes import byref, c_long, c_short, c_void_p, sizeof, windll, wintypes

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget
import pywintypes
import win32api
import win32con
import win32gui

from app_info import APP_NAME
from ui_icons import stroke_icon


FRAME_RADIUS = 14
FRAME_BORDER = 0xD8D8D8
DWMWA_NCRENDERING_POLICY = 2
DWMWA_BORDER_COLOR = 34
DWMNCRP_DISABLED = 1
DWMWA_COLOR_NONE = 0xFFFFFFFE
SHADOW_SPREAD = 12
SHADOW_OFFSET = 3
SHADOW_MARGIN = SHADOW_SPREAD + SHADOW_OFFSET + 1

dwm = windll.dwmapi
dwm.DwmSetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD)
dwm.DwmSetWindowAttribute.restype = c_long


def set_dwm_value(hwnd, attribute, value):
    setting = wintypes.DWORD(value)
    dwm.DwmSetWindowAttribute(hwnd, attribute, byref(setting), sizeof(setting))


def hide_native_caption(hwnd):
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    if style & win32con.WS_CAPTION:
        # Keep the sizing frame so Windows can resize and snap the window.
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style & ~win32con.WS_CAPTION)
        win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                             win32con.SWP_FRAMECHANGED | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                             win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)


class WindowShadow(QWidget):
    def __init__(self, owner):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint |
                 Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(owner, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.owner_bounds = QRectF()

    def follow(self, window):
        frame = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        self.setGeometry(frame.adjusted(-SHADOW_MARGIN, -SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN))
        if not self.isVisible():
            self.show()
        left, top, right, bottom = win32gui.GetWindowRect(int(window.winId()))
        shadow_left, shadow_top, _, _ = win32gui.GetWindowRect(int(self.winId()))
        scale = self.devicePixelRatioF()
        bounds = QRectF((left - shadow_left) / scale, (top - shadow_top) / scale,
                        (right - left) / scale, (bottom - top) / scale)
        if bounds != self.owner_bounds:
            self.owner_bounds = bounds
            self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            if self.owner_bounds.isEmpty():
                return
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            frame = self.owner_bounds
            # The owned shadow window sits above its owner. Leave the body and outline transparent.
            cutout = QPainterPath()
            cutout.addRect(QRectF(self.rect()))
            cutout.addRoundedRect(frame.adjusted(-0.25, -0.25, 0.25, 0.25), FRAME_RADIUS, FRAME_RADIUS)
            painter.setClipPath(cutout)
            source = frame.translated(0, SHADOW_OFFSET)
            for spread in range(SHADOW_SPREAD, 0, -1):
                strength = max(1, 7 * (SHADOW_SPREAD - spread + 1) // SHADOW_SPREAD)
                painter.setBrush(QColor(0, 0, 0, strength))
                radius = FRAME_RADIUS + spread
                painter.drawRoundedRect(source.adjusted(-spread, -spread, spread, spread), radius, radius)


class WindowDecoration(QWidget):
    """Keep the native clipping region, outline, and shadow aligned."""

    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hwnd = None
        self.region_shape = None
        self.shadow = WindowShadow(window)
        self.hide()
        window.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and QGuiApplication.platformName() == "windows":
            kind = event.type()
            if kind == QEvent.Type.WinIdChange:
                hwnd = obj.effectiveWinId()
                if hwnd:
                    hide_native_caption(hwnd)
            elif kind == QEvent.Type.Hide or (kind == QEvent.Type.WindowStateChange and obj.isMinimized()):
                self.shadow.hide()
            elif kind == QEvent.Type.Move and self.shadow.isVisible():
                self.shadow.follow(obj)
            elif (kind in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.WindowStateChange,
                           QEvent.Type.DevicePixelRatioChange) and obj.isVisible() and not obj.isMinimized()):
                self.refresh_frame(obj)
        return super().eventFilter(obj, event)

    def refresh_frame(self, window):
        hwnd = int(window.winId())
        hide_native_caption(hwnd)
        docked = window.isMaximized() or window.isFullScreen()
        if hwnd != self.hwnd:
            self.hwnd = hwnd
            self.region_shape = None
            set_dwm_value(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
            set_dwm_value(hwnd, DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED)

        if docked:
            self.shadow.hide()
            if self.region_shape is not None:
                win32gui.SetWindowRgn(hwnd, 0, True)
                self.region_shape = None
            self.hide()
            return

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        shape = (right - left + 1, bottom - top + 1, round(2 * FRAME_RADIUS * window.devicePixelRatioF()))
        if shape != self.region_shape:
            width, height, diameter = shape
            region = win32gui.CreateRoundRectRgn(0, 0, width, height, diameter, diameter)
            try:
                win32gui.SetWindowRgn(hwnd, region, True)
            except pywintypes.error:
                win32gui.DeleteObject(region)
                return
            self.region_shape = shape  # Windows owns the region after SetWindowRgn succeeds.
        self.setGeometry(window.rect())
        self.raise_()
        self.show()
        if not window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.shadow.follow(window)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor.fromRgb(FRAME_BORDER), 1))
            frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            # Inset the stroke by half its width to follow the window's clipping edge.
            radius = FRAME_RADIUS - 0.5
            painter.drawRoundedRect(frame, radius, radius)


class AppLogo(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("appLogo")
        self.setFixedSize(22, 22)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            self.window().windowIcon().paint(painter, self.rect())


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(46)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 8, 0)
        row.setSpacing(8)
        row.addWidget(AppLogo())
        name = QLabel(APP_NAME)
        name.setObjectName("titleBarName")
        row.addWidget(name)
        row.addStretch(1)

        controls = QHBoxLayout()
        controls.setSpacing(2)
        controls.addWidget(self.button("minimizeWindow", "最小化", "minimize", window.showMinimized))
        self.full_screen = self.button("fullScreenWindow", "全屏", "fullScreen", window.toggle_full_screen)
        controls.addWidget(self.full_screen)
        controls.addWidget(self.button("closeWindow", "关闭窗口", "close", window.close))
        row.addLayout(controls)

        shortcut = QShortcut(QKeySequence(Qt.Key.Key_F11), window)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(window.toggle_full_screen)

    def button(self, object_name, description, icon, action):
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setToolTip(description)
        button.setAccessibleName(description)
        button.setIcon(stroke_icon(icon))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(40, 32)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(action)
        return button

    def show_full_screen_state(self, full_screen):
        description = "退出全屏" if full_screen else "全屏"
        self.full_screen.setToolTip(description)
        self.full_screen.setAccessibleName(description)
        self.full_screen.setIcon(stroke_icon("exitFullScreen" if full_screen else "fullScreen"))

    def can_drag_at(self, point):
        return self.rect().contains(point) and not isinstance(self.childAt(point), QToolButton)


def window_frame_result(window, title_bar, event_type, address):
    if event_type != b"windows_generic_MSG":
        return None
    message = wintypes.MSG.from_address(int(address))
    if message.message not in (win32con.WM_NCCALCSIZE, win32con.WM_NCHITTEST):
        return None
    hwnd = message.hWnd
    maximized = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & win32con.WS_MAXIMIZE)
    if message.message == win32con.WM_NCCALCSIZE and message.wParam:
        # Keep the native window style, but give its caption and frame space to Qt.
        if maximized and not window.isFullScreen():
            rect = wintypes.RECT.from_address(message.lParam)
            monitor = win32api.MonitorFromRect((rect.left, rect.top, rect.right, rect.bottom),
                                             win32con.MONITOR_DEFAULTTONEAREST)
            rect.left, rect.top, rect.right, rect.bottom = win32api.GetMonitorInfo(monitor)["Work"]
        return 0

    if message.message != win32con.WM_NCHITTEST or title_bar is None:
        return None
    if window.isFullScreen():
        return win32con.HTCLIENT

    screen_point = (c_short(win32api.LOWORD(message.lParam)).value,
                    c_short(win32api.HIWORD(message.lParam)).value)
    x, y = win32gui.ScreenToClient(hwnd, screen_point)
    scale = window.devicePixelRatioF()
    if not maximized:
        _, _, width, height = win32gui.GetClientRect(hwnd)
        margin = round(6 * scale)
        horizontal = win32con.HTLEFT if x < margin else win32con.HTRIGHT if x >= width - margin else 0
        vertical = win32con.HTTOP if y < margin else win32con.HTBOTTOM if y >= height - margin else 0
        corners = {(win32con.HTLEFT, win32con.HTTOP): win32con.HTTOPLEFT,
                   (win32con.HTRIGHT, win32con.HTTOP): win32con.HTTOPRIGHT,
                   (win32con.HTLEFT, win32con.HTBOTTOM): win32con.HTBOTTOMLEFT,
                   (win32con.HTRIGHT, win32con.HTBOTTOM): win32con.HTBOTTOMRIGHT}
        if horizontal or vertical:
            return corners.get((horizontal, vertical), horizontal or vertical)

    point = title_bar.mapFrom(window, QPoint(int(x / scale), int(y / scale)))
    return win32con.HTCAPTION if title_bar.can_drag_at(point) else win32con.HTCLIENT
