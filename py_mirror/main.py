import sys
import os
import subprocess
import threading
import time
import queue
import math
import av
import urllib.parse

from PyQt6.QtWidgets import (QApplication, QLabel, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QDialog, QListWidget, 
                             QMessageBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint, QTimer
from PyQt6.QtGui import QImage, QPixmap, QKeyEvent, QIcon

# Configuration
# 720x1600 is the most stable resolution for USB 2.0
STREAM_WIDTH = 720
STREAM_HEIGHT = 1600
BYTES_PER_PIXEL = 3
FRAME_SIZE = STREAM_WIDTH * STREAM_HEIGHT * BYTES_PER_PIXEL

# --- Helper: Non-Seekable Stream Wrapper ---
class NonSeekableStream:
    """
    Wraps a stream (like a pipe) to explicitly deny seeking.
    Fixes 'OSError: [Errno 22] Invalid argument' in PyAV on Windows.
    """
    def __init__(self, stream):
        self.stream = stream
        self.total_read = 0

    def read(self, n):
        data = self.stream.read(n)
        return data

    def seekable(self):
        return False

# --- 1. ADB Command Processor (Prevents Crashing) ---
class ADBWorker(QThread):
    """
    Processes ADB commands sequentially to prevent spawning 
    thousands of threads and crashing the system.
    """
    status_signal = pyqtSignal(str) # For UI feedback

    def __init__(self, adb_path, device_serial=None):
        super().__init__()
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.queue = queue.Queue()
        self._running = True

    def _get_base_cmd(self):
        cmd = [self.adb_path]
        if self.device_serial:
            cmd.extend(['-s', self.device_serial])
        return cmd

    def run(self):
        while self._running:
            try:
                # Get command from queue (blocks until available)
                task = self.queue.get()
                if task is None: break
                
                cmd_type, data = task
                
                if cmd_type == 'shell':
                    # Fast execution for input events
                    full_cmd = self._get_base_cmd() + ['shell'] + data
                    subprocess.run(full_cmd, stdout=subprocess.DEVNULL)
                    
                elif cmd_type == 'push':
                    local_path, remote_path = data
                    self.status_signal.emit(f"Pushing {os.path.basename(local_path)}...")
                    
                    push_cmd = self._get_base_cmd() + ['push', local_path, remote_path]
                    subprocess.run(push_cmd)
                    
                    # Refresh Gallery
                    self.status_signal.emit("Refreshing Gallery...")
                    # Encode path for Android shell (handles spaces, Greek chars, etc.)
                    # safe='/' ensures we don't encode the directory separators
                    encoded_path = urllib.parse.quote(remote_path, safe='/')
                    uri = f"file://{encoded_path}"
                    
                    scan_cmd = self._get_base_cmd() + [
                        'shell', 'am', 'broadcast', 
                        '-a', 'android.intent.action.MEDIA_SCANNER_SCAN_FILE', 
                        '-d', uri
                    ]
                    subprocess.run(scan_cmd, stdout=subprocess.DEVNULL)
                    
                    # Share File (Workaround for Viber/WhatsApp transfer)
                    self.status_signal.emit("Sharing File...")
                    self.share_file(remote_path)
                    
                    time.sleep(1)
                    self.status_signal.emit("Ready")
                
                elif cmd_type == 'text':
                    # Handle text input
                    text = data
                    if not text: continue
                    
                    # Check for non-ASCII (Greek, etc) which crashes 'input text'
                    if not text.isascii():
                        print(f"Ignored non-ASCII text: {text} (Android 'input text' does not support it)")
                        continue

                    # ADB input text requires %s for spaces
                    # and escaping for shell special chars
                    escaped = text.replace(' ', '%s').replace("'", r"\'").replace('"', r'\"').replace('(', r'\(').replace(')', r'\)')
                    
                    text_cmd = self._get_base_cmd() + ['shell', 'input', 'text', escaped]
                    subprocess.run(text_cmd, stdout=subprocess.DEVNULL)

                self.queue.task_done()
            except Exception as e:
                print(f"[Worker Error] {e}")

    def share_file(self, path):
        # Auto-detect type
        ext = os.path.splitext(path)[1].lower()
        mime = "*/*"
        if ext in ['.jpg', '.jpeg', '.png']: mime = "image/*"
        elif ext in ['.mp4', '.mkv']: mime = "video/*"
        elif ext in ['.pdf']: mime = "application/pdf"
        elif ext in ['.txt']: mime = "text/plain"
        
        encoded_path = urllib.parse.quote(path, safe='/')
        uri = f"file://{encoded_path}"
        
        # Use ACTION_SEND to trigger the Share Sheet (Viber, WhatsApp, etc.)
        cmd = self._get_base_cmd() + [
            'shell', 'am', 'start',
            '-a', 'android.intent.action.SEND',
            '-t', mime,
            '--eu', 'android.intent.extra.STREAM', uri
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL)

    def add_cmd(self, args):
        self.queue.put(('shell', args))
        
    def add_text(self, text):
        self.queue.put(('text', text))

    def add_push(self, local, remote):
        self.queue.put(('push', (local, remote)))

    def stop(self):
        self._running = False
        self.queue.put(None)

# --- 2. Video Stream Thread ---
class VideoThread(QThread):
    change_pixmap_signal = pyqtSignal(QImage)

    def __init__(self, adb_path, device_serial=None, size=None):
        super().__init__()
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.native_size = size if size else (720, 1600)
        self._run_flag = True

    def run(self):
        # Calculate optimal resolution (Max 1080p width, keep aspect ratio)
        w, h = self.native_size
        if w > 1080:
            scale = 1080 / w
            w = 1080
            h = int(h * scale)
        
        # Ensure dimensions are even (required by H.264)
        w = w - (w % 2)
        h = h - (h % 2)
        
        # Update global stream size for coordinate mapping logic (approximate)
        # Note: Ideally coordinate mapping should be dynamic too, but for now 
        # the scale factors in MirrorWindow update_image handle it relative to window size.
        print(f"Streaming Resolution: {w}x{h} @ 8Mbps")

        base_cmd = [self.adb_path]
        if self.device_serial:
            base_cmd.extend(['-s', self.device_serial])
            
        adb_cmd = base_cmd + [
            'exec-out', 
            'screenrecord', 
            '--output-format=h264', 
            f'--size={w}x{h}', 
            '--bit-rate=8000000', 
            '--time-limit=0',
            '-'
        ]

        print("Starting Video Stream...")

        while self._run_flag:
            adb = None
            try:
                # bufsize=64KB: Reasonable buffer for video stream
                adb = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=65536)
                
                # Check if process started immediate failure
                if adb.poll() is not None:
                    print("ADB Video Process failed to start.")
                    time.sleep(1)
                    continue

                # Open PyAV container
                try:
                    # Wrap stdout to prevent PyAV from trying to seek on the pipe
                    wrapped_stream = NonSeekableStream(adb.stdout)
                    
                    container = av.open(
                        wrapped_stream, 
                        format='h264',
                        options={
                            'flags': 'low_delay',
                            'threads': 'auto',      # Enable multi-threaded decoding
                            'probesize': '102400',  # 100KB: Sufficient for headers
                            'analyzeduration': '0'
                        }
                    )
                except Exception:
                    time.sleep(1)
                    continue
                
                for frame in container.decode(video=0):
                    if not self._run_flag:
                        break
                    
                    # Convert to RGB
                    img = frame.to_rgb()
                    # Get the raw plane data
                    ptr = img.planes[0]
                    
                    # Create QImage
                    qt_image = QImage(
                        ptr, 
                        img.width, 
                        img.height, 
                        img.planes[0].line_size, 
                        QImage.Format.Format_RGB888
                    )
                    
                    self.change_pixmap_signal.emit(qt_image.copy())

            except Exception as e:
                print(f"Video Error: {e}")
                time.sleep(1)
            finally:
                if adb:
                    adb.terminate()

    def stop(self):
        self._run_flag = False
        self.wait()

# --- 3. Main Window with Gestures ---
class MirrorWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyMirror Ultimate")
        self.resize(360, 850) # Increased height for navbar
        self.setAcceptDrops(True)

        # Main Layout
        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setSpacing(0)
        self.setLayout(self.layout)

        # Video Area
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background-color: black;")
        self.layout.addWidget(self.label, stretch=1)

        # Navigation Bar
        self.nav_bar = QWidget()
        self.nav_bar.setFixedHeight(50)
        self.nav_bar.setStyleSheet("background-color: #222;")
        self.nav_layout = QHBoxLayout()
        self.nav_layout.setContentsMargins(20, 5, 20, 5)
        self.nav_bar.setLayout(self.nav_layout)
        
        # Nav Buttons
        btn_style = """
            QPushButton {
                background-color: transparent;
                color: white;
                font-size: 16px;
                border: none;
                font-weight: bold;
            }
            QPushButton:pressed {
                color: #aaa;
            }
        """
        
        self.btn_back = QPushButton("◀") # Back
        self.btn_home = QPushButton("●") # Home
        self.btn_recents = QPushButton("■") # Recents
        self.btn_vol_down = QPushButton("🔉") # Vol Down
        self.btn_vol_up = QPushButton("🔊") # Vol Up
        
        # Add buttons to layout
        # Order: Back, Home, Recents, Spacer, Vol-, Vol+
        self.nav_layout.addWidget(self.btn_back)
        self.nav_layout.addWidget(self.btn_home)
        self.nav_layout.addWidget(self.btn_recents)
        
        # Add a flexible spacer to separate nav and volume
        spacer = QLabel()
        spacer.setStyleSheet("background: transparent;")
        self.nav_layout.addWidget(spacer, stretch=1)
        
        self.nav_layout.addWidget(self.btn_vol_down)
        self.nav_layout.addWidget(self.btn_vol_up)
        
        for btn in [self.btn_back, self.btn_home, self.btn_recents, self.btn_vol_down, self.btn_vol_up]:
            btn.setStyleSheet(btn_style)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            
        self.layout.addWidget(self.nav_bar)

        # Tools
        self.adb_path = self.find_tool("adb", "adb_tools/platform-tools/adb.exe")
        print(f"ADB Path: {self.adb_path}")
        
        # Device Selection
        self.device_serial = self.select_device()
        if not self.device_serial:
            print("No device selected or found.")
            # We don't exit here, might be waiting for connection, 
            # but for now let's assume single device default behavior if none picked
            # or just continue and let adb handle it (which might fail if >1)
        
        # Get Real Device Resolution for Input Scaling
        self.device_w, self.device_h = self.get_device_resolution()
        print(f"Device Resolution: {self.device_w}x{self.device_h}")

        # Worker for Input
        self.worker = ADBWorker(self.adb_path, self.device_serial)
        self.worker.status_signal.connect(self.setWindowTitle)
        self.worker.start()
        
        # Connect Nav Buttons
        self.btn_back.clicked.connect(lambda: self.worker.add_cmd(['input', 'keyevent', '4']))
        self.btn_home.clicked.connect(lambda: self.worker.add_cmd(['input', 'keyevent', '3']))
        self.btn_recents.clicked.connect(lambda: self.worker.add_cmd(['input', 'keyevent', '187']))
        self.btn_vol_down.clicked.connect(lambda: self.worker.add_cmd(['input', 'keyevent', '25']))
        self.btn_vol_up.clicked.connect(lambda: self.worker.add_cmd(['input', 'keyevent', '24']))

        # Video
        self.video_thread = VideoThread(self.adb_path, self.device_serial, (self.device_w, self.device_h))
        self.video_thread.change_pixmap_signal.connect(self.update_image)
        self.video_thread.start()

        # Input State
        self.start_pos = None
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.offset_x = 0
        self.offset_y = 0
        
        # Long Press Timer
        self.long_press_timer = QTimer()
        self.long_press_timer.setInterval(600) # 600ms hold
        self.long_press_timer.setSingleShot(True)
        self.long_press_timer.timeout.connect(self.handle_long_press)
        self.is_long_press = False

    def find_tool(self, name, rel):
        # 1. Try relative to this script file (Most Reliable)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path1 = os.path.join(script_dir, rel.replace('/', os.sep))
        if os.path.exists(path1): return path1
        
        # 2. Try relative to CWD (Current Working Directory)
        # If running from project root: ./py_mirror/adb_tools/...
        path2 = os.path.join(os.getcwd(), "py_mirror", rel.replace('/', os.sep))
        if os.path.exists(path2): return path2
        
        # 3. Try CWD directly
        # If running from inside py_mirror: ./adb_tools/...
        path3 = os.path.join(os.getcwd(), rel.replace('/', os.sep))
        if os.path.exists(path3): return path3

        print(f"WARNING: Bundled tool '{name}' not found at {path1}. Using system default.")
        return name

    def select_device(self):
        try:
            # List devices
            out = subprocess.check_output([self.adb_path, 'devices', '-l']).decode('utf-8')
            lines = out.strip().split('\n')[1:] # Skip header
            devices = []
            for line in lines:
                if 'device' in line and 'product:' in line:
                    parts = line.split()
                    serial = parts[0]
                    # Try to find model:product:
                    model = "Unknown"
                    for p in parts:
                        if p.startswith("model:"):
                            model = p.split(':')[1]
                    devices.append({'serial': serial, 'model': model})
            
            if len(devices) == 0:
                return None
            elif len(devices) == 1:
                print(f"Auto-selecting single device: {devices[0]['model']}")
                return devices[0]['serial']
            else:
                # Multiple devices
                picker = DevicePicker(devices)
                if picker.exec() == QDialog.DialogCode.Accepted:
                    return picker.selected_serial
                return None # User cancelled
                
        except Exception as e:
            print(f"Error enumerating devices: {e}")
            return None

    def get_device_resolution(self):
        try:
            cmd = [self.adb_path]
            if self.device_serial:
                cmd.extend(['-s', self.device_serial])
            cmd.extend(['shell', 'wm', 'size'])
            
            res = subprocess.check_output(cmd).decode('utf-8')
            # Output format: "Physical size: 1220x2712"
            if 'Physical size:' in res:
                parts = res.split(':')[1].strip().split('x')
                return int(parts[0]), int(parts[1])
        except Exception as e:
            print(f"Failed to get resolution: {e}")
        return STREAM_WIDTH, STREAM_HEIGHT

    def update_image(self, qt_image):
        pix = QPixmap.fromImage(qt_image)
        w_win, h_win = self.label.width(), self.label.height()
        
        # Use FastTransformation for performance (SmoothTransformation is too slow for video)
        scaled = pix.scaled(
            w_win, 
            h_win, 
            Qt.AspectRatioMode.KeepAspectRatio, 
            Qt.TransformationMode.FastTransformation
        )
        self.label.setPixmap(scaled)

        # Update scaling math
        if scaled.width() > 0:
            # Scale from Window -> Stream (720p)
            self.scale_x = STREAM_WIDTH / scaled.width()
            self.scale_y = STREAM_HEIGHT / scaled.height()
            self.offset_x = (w_win - scaled.width()) // 2
            self.offset_y = (h_win - scaled.height()) // 2

    # --- Gesture Handling ---
    def get_coords(self, pos):
        # 1. Convert Window coords -> Stream coords (720x1600)
        x_stream = (pos.x() - self.offset_x) * self.scale_x
        y_stream = (pos.y() - self.offset_y) * self.scale_y
        
        # 2. Convert Stream coords -> Real Device coords
        x_real = x_stream * (self.device_w / STREAM_WIDTH)
        y_real = y_stream * (self.device_h / STREAM_HEIGHT)
        
        return int(x_real), int(y_real)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_pos = event.pos()
            self.is_long_press = False
            self.long_press_timer.start()
        elif event.button() == Qt.MouseButton.RightButton:
            # Android Back Key (Keycode 4)
            self.worker.add_cmd(['input', 'keyevent', '4'])

    def handle_long_press(self):
        if self.start_pos:
            self.is_long_press = True
            x, y = self.get_coords(self.start_pos)
            # Simulate long press with a stationary swipe for 1000ms
            self.worker.add_cmd(['input', 'swipe', str(x), str(y), str(x), str(y), '1000'])
            print("Long Press Triggered")

    def mouseMoveEvent(self, event):
        # If we move too much, cancel long press
        if self.start_pos and not self.is_long_press:
            dist = (event.pos() - self.start_pos).manhattanLength()
            if dist > 10:
                self.long_press_timer.stop()

    def mouseReleaseEvent(self, event):
        self.long_press_timer.stop()
        
        if event.button() == Qt.MouseButton.LeftButton and self.start_pos:
            # If long press already handled, do nothing
            if self.is_long_press:
                self.start_pos = None
                return

            end_pos = event.pos()
            x1, y1 = self.get_coords(self.start_pos)
            x2, y2 = self.get_coords(end_pos)
            
            # Calculate distance (in device pixels now)
            dist = math.sqrt((x2-x1)**2 + (y2-y1)**2)
            
            # Threshold adjusted for higher density screens (approx 50px)
            if dist < 50: 
                # It's a TAP
                self.worker.add_cmd(['input', 'tap', str(x1), str(y1)])
            else:
                # It's a SWIPE (Duration 300ms)
                self.worker.add_cmd(['input', 'swipe', str(x1), str(y1), str(x2), str(y2), '300'])
            
            self.start_pos = None

    def keyPressEvent(self, event):
        # Key Mapping
        key = event.key()
        text = event.text()
        modifiers = event.modifiers()
        
        # Paste (Ctrl+V)
        if (modifiers & Qt.KeyboardModifier.ControlModifier) and key == Qt.Key.Key_V:
            clipboard_text = QApplication.clipboard().text()
            if clipboard_text:
                print(f"Pasting: {clipboard_text[:20]}...")
                self.worker.add_text(clipboard_text)
            return
        
        # Special Keys
        if key == Qt.Key.Key_Backspace:
            self.worker.add_cmd(['input', 'keyevent', '67'])
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            self.worker.add_cmd(['input', 'keyevent', '66'])
        elif key == Qt.Key.Key_Escape:
            self.worker.add_cmd(['input', 'keyevent', '4']) # Back
        elif key == Qt.Key.Key_Tab:
            self.worker.add_cmd(['input', 'keyevent', '61'])
        elif key == Qt.Key.Key_Left:
            self.worker.add_cmd(['input', 'keyevent', '21'])
        elif key == Qt.Key.Key_Right:
            self.worker.add_cmd(['input', 'keyevent', '22'])
        elif key == Qt.Key.Key_Up:
            self.worker.add_cmd(['input', 'keyevent', '19'])
        elif key == Qt.Key.Key_Down:
            self.worker.add_cmd(['input', 'keyevent', '20'])
        elif key == Qt.Key.Key_Space:
            self.worker.add_cmd(['input', 'keyevent', '62'])
        
        # Printable text (including Greek)
        elif text and text.isprintable():
            self.worker.add_text(text)

    def wheelEvent(self, event):
        # Scroll logic
        delta = event.angleDelta().y()
        x, y = self.get_coords(event.position())
        
        # Reduced scroll distance (150px)
        scroll_dist = 150
        
        # Scroll Down (Swipe Up)
        if delta < 0:
            self.worker.add_cmd(['input', 'swipe', str(x), str(y+scroll_dist), str(x), str(y-scroll_dist), '100'])
        # Scroll Up (Swipe Down)
        else:
            self.worker.add_cmd(['input', 'swipe', str(x), str(y-scroll_dist), str(x), str(y+scroll_dist), '100'])

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for f in files:
            # Sanitize filename for Android compatibility
            # Keep extension, replace everything else with safe chars
            base_name = os.path.basename(f)
            name, ext = os.path.splitext(base_name)
            
            # Create a safe alphanumeric name with timestamp to avoid collisions
            # and ensure ASCII-only characters for ADB stability
            safe_name = "".join([c if (c.isascii() and c.isalnum()) or c in ('-', '_') else '_' for c in name])
            if not safe_name: safe_name = "file"
            
            # Limit length
            safe_name = safe_name[:50]
            
            remote_name = f"{int(time.time())}_{safe_name}{ext}"
            remote = f"/sdcard/Download/{remote_name}"
            
            print(f"Transferring: {base_name} -> {remote}")
            self.worker.add_push(f, remote)

    def dragEnterEvent(self, e): e.accept()

    def closeEvent(self, e):
        self.worker.stop()
        self.video_thread.stop()
        e.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MirrorWindow()
    win.show()
    sys.exit(app.exec())
