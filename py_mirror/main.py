import sys
import os
import subprocess
import threading
import time
import queue
import math
import av
import urllib.parse

from PyQt6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint, QTimer
from PyQt6.QtGui import QImage, QPixmap, QKeyEvent

# Configuration
# 720x1600 is the most stable resolution for USB 2.0
STREAM_WIDTH = 720
STREAM_HEIGHT = 1600
BYTES_PER_PIXEL = 3
FRAME_SIZE = STREAM_WIDTH * STREAM_HEIGHT * BYTES_PER_PIXEL

# --- 1. ADB Command Processor (Prevents Crashing) ---
class ADBWorker(QThread):
    """
    Processes ADB commands sequentially to prevent spawning 
    thousands of threads and crashing the system.
    """
    status_signal = pyqtSignal(str) # For UI feedback

    def __init__(self, adb_path):
        super().__init__()
        self.adb_path = adb_path
        self.queue = queue.Queue()
        self._running = True

    def run(self):
        while self._running:
            try:
                # Get command from queue (blocks until available)
                task = self.queue.get()
                if task is None: break
                
                cmd_type, data = task
                
                if cmd_type == 'shell':
                    # Fast execution for input events
                    full_cmd = [self.adb_path, 'shell'] + data
                    subprocess.run(full_cmd, stdout=subprocess.DEVNULL)
                    
                elif cmd_type == 'push':
                    local_path, remote_path = data
                    self.status_signal.emit(f"Pushing {os.path.basename(local_path)}...")
                    subprocess.run([self.adb_path, 'push', local_path, remote_path])
                    
                    # Refresh Gallery
                    self.status_signal.emit("Refreshing Gallery...")
                    # Encode path for Android shell (handles spaces, Greek chars, etc.)
                    # safe='/' ensures we don't encode the directory separators
                    encoded_path = urllib.parse.quote(remote_path, safe='/')
                    uri = f"file://{encoded_path}"
                    
                    subprocess.run([
                        self.adb_path, 'shell', 'am', 'broadcast', 
                        '-a', 'android.intent.action.MEDIA_SCANNER_SCAN_FILE', 
                        '-d', uri
                    ], stdout=subprocess.DEVNULL)
                    
                    # Open File
                    self.status_signal.emit("Opening File...")
                    self.open_file(remote_path)
                    
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
                    subprocess.run(
                        [self.adb_path, 'shell', 'input', 'text', escaped], 
                        stdout=subprocess.DEVNULL
                    )

                self.queue.task_done()
            except Exception as e:
                print(f"[Worker Error] {e}")

    def open_file(self, path):
        # Auto-detect type
        ext = os.path.splitext(path)[1].lower()
        mime = "*/*"
        if ext in ['.jpg', '.png']: mime = "image/*"
        elif ext in ['.mp4']: mime = "video/*"
        
        encoded_path = urllib.parse.quote(path, safe='/')
        uri = f"file://{encoded_path}"
        
        subprocess.run([
            self.adb_path, 'shell', 'am', 'start',
            '-a', 'android.intent.action.VIEW',
            '-d', uri,
            '-t', mime
        ], stdout=subprocess.DEVNULL)

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

    def __init__(self, adb_path):
        super().__init__()
        self.adb_path = adb_path
        self._run_flag = True

    def run(self):
        adb_cmd = [
            self.adb_path, 'exec-out', 
            'screenrecord', 
            '--output-format=h264', 
            f'--size={STREAM_WIDTH}x{STREAM_HEIGHT}', 
            '--bit-rate=8000000', 
            '-'
        ]

        while self._run_flag:
            adb = None
            try:
                # Use a larger buffer for high resolution
                adb = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=2*10**7)
                
                # Open PyAV container
                # We use format='h264' because the input is raw h264 stream
                # 'nobuffer': reduce latency
                # 'flags': 'low_delay' tells the decoder to output frames immediately
                container = av.open(
                    adb.stdout, 
                    format='h264',
                    options={'fflags': 'nobuffer', 'flags': 'low_delay'}
                )
                
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
        self.resize(360, 800)
        self.setAcceptDrops(True)

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(0,0,0,0)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(self.label)
        self.setLayout(self.layout)

        # Tools
        self.adb_path = self.find_tool("adb", "adb_tools/platform-tools/adb.exe")
        
        # Get Real Device Resolution for Input Scaling
        self.device_w, self.device_h = self.get_device_resolution()
        print(f"Device Resolution: {self.device_w}x{self.device_h}")

        # Worker for Input
        self.worker = ADBWorker(self.adb_path)
        self.worker.status_signal.connect(self.setWindowTitle)
        self.worker.start()

        # Video
        self.video_thread = VideoThread(self.adb_path)
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
        loc = os.path.join(os.getcwd(), "py_mirror", rel.replace('/', os.sep))
        return loc if os.path.exists(loc) else name

    def get_device_resolution(self):
        try:
            # Run adb shell wm size
            res = subprocess.check_output([self.adb_path, 'shell', 'wm', 'size']).decode('utf-8')
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
        
        # Use SmoothTransformation for high-quality downscaling
        scaled = pix.scaled(
            w_win, 
            h_win, 
            Qt.AspectRatioMode.KeepAspectRatio, 
            Qt.TransformationMode.SmoothTransformation
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
            safe_name = "".join([c if c.isalnum() or c in ('-', '_') else '_' for c in name])
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
