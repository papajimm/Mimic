import sys
import os
import subprocess
import threading
import time
import queue
import math

from PyQt6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QImage, QPixmap

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
                    subprocess.run([
                        self.adb_path, 'shell', 'am', 'broadcast', 
                        '-a', 'android.intent.action.MEDIA_SCANNER_SCAN_FILE', 
                        '-d', f'file://{remote_path}'
                    ], stdout=subprocess.DEVNULL)
                    
                    # Open File
                    self.status_signal.emit("Opening File...")
                    self.open_file(remote_path)
                    
                    time.sleep(1)
                    self.status_signal.emit("Ready")

                self.queue.task_done()
            except Exception as e:
                print(f"[Worker Error] {e}")

    def open_file(self, path):
        # Auto-detect type
        ext = os.path.splitext(path)[1].lower()
        mime = "*/*"
        if ext in ['.jpg', '.png']: mime = "image/*"
        elif ext in ['.mp4']: mime = "video/*"
        
        subprocess.run([
            self.adb_path, 'shell', 'am', 'start',
            '-a', 'android.intent.action.VIEW',
            '-d', f'file://{path}',
            '-t', mime
        ], stdout=subprocess.DEVNULL)

    def add_cmd(self, args):
        self.queue.put(('shell', args))

    def add_push(self, local, remote):
        self.queue.put(('push', (local, remote)))

    def stop(self):
        self._running = False
        self.queue.put(None)

# --- 2. Video Stream Thread ---
class VideoThread(QThread):
    change_pixmap_signal = pyqtSignal(QImage)

    def __init__(self, adb_path, ffmpeg_path):
        super().__init__()
        self.adb_path = adb_path
        self.ffmpeg_path = ffmpeg_path
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

        # Stable decoding flags
        ffmpeg_cmd = [
            self.ffmpeg_path,
            '-i', '-', 
            '-f', 'rawvideo', 
            '-pix_fmt', 'rgb24', 
            '-vcodec', 'rawvideo',
            '-tune', 'zerolatency',
            '-preset', 'ultrafast',
            '-'
        ]

        while self._run_flag:
            try:
                adb = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)
                ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=adb.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)

                while self._run_flag:
                    raw_data = ffmpeg.stdout.read(FRAME_SIZE)
                    if not raw_data or len(raw_data) != FRAME_SIZE:
                        break
                    
                    image = QImage(raw_data, STREAM_WIDTH, STREAM_HEIGHT, STREAM_WIDTH*3, QImage.Format.Format_RGB888)
                    self.change_pixmap_signal.emit(image.copy())

                adb.terminate()
                ffmpeg.terminate()
            except:
                time.sleep(1)

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
        self.ffmpeg_path = self.find_tool("ffmpeg", "ffmpeg/ffmpeg.exe")

        # Worker for Input
        self.worker = ADBWorker(self.adb_path)
        self.worker.status_signal.connect(self.setWindowTitle)
        self.worker.start()

        # Video
        self.video_thread = VideoThread(self.adb_path, self.ffmpeg_path)
        self.video_thread.change_pixmap_signal.connect(self.update_image)
        self.video_thread.start()

        # Input State
        self.start_pos = None
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.offset_x = 0
        self.offset_y = 0

    def find_tool(self, name, rel):
        loc = os.path.join(os.getcwd(), "py_mirror", rel.replace('/', os.sep))
        return loc if os.path.exists(loc) else name

    def update_image(self, qt_image):
        pix = QPixmap.fromImage(qt_image)
        w_win, h_win = self.label.width(), self.label.height()
        scaled = pix.scaled(w_win, h_win, Qt.AspectRatioMode.KeepAspectRatio)
        self.label.setPixmap(scaled)

        # Update scaling math
        if scaled.width() > 0:
            self.scale_x = STREAM_WIDTH / scaled.width()
            self.scale_y = STREAM_HEIGHT / scaled.height()
            self.offset_x = (w_win - scaled.width()) // 2
            self.offset_y = (h_win - scaled.height()) // 2

    # --- Gesture Handling ---
    def get_coords(self, pos):
        # Convert Window coords -> Stream coords
        x = (pos.x() - self.offset_x) * self.scale_x
        y = (pos.y() - self.offset_y) * self.scale_y
        return int(x), int(y)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_pos = event.pos()
        elif event.button() == Qt.MouseButton.RightButton:
            # Android Back Key (Keycode 4)
            self.worker.add_cmd(['input', 'keyevent', '4'])

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start_pos:
            end_pos = event.pos()
            x1, y1 = self.get_coords(self.start_pos)
            x2, y2 = self.get_coords(end_pos)
            
            # Calculate distance
            dist = math.sqrt((x2-x1)**2 + (y2-y1)**2)
            
            if dist < 20: 
                # It's a TAP
                self.worker.add_cmd(['input', 'tap', str(x1), str(y1)])
            else:
                # It's a SWIPE (Duration 300ms)
                self.worker.add_cmd(['input', 'swipe', str(x1), str(y1), str(x2), str(y2), '300'])
            
            self.start_pos = None

    def wheelEvent(self, event):
        # Scroll logic
        delta = event.angleDelta().y()
        x, y = self.get_coords(event.position())
        
        # Scroll Down (Swipe Up)
        if delta < 0:
            self.worker.add_cmd(['input', 'swipe', str(x), str(y+300), str(x), str(y-300), '100'])
        # Scroll Up (Swipe Down)
        else:
            self.worker.add_cmd(['input', 'swipe', str(x), str(y-300), str(x), str(y+300), '100'])

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for f in files:
            remote = f"/sdcard/Download/{os.path.basename(f)}"
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
