# PyMirror Ultimate

A lightweight, Python-based Android screen mirroring tool (Vysor alternative) with low latency, high quality, and file transfer capabilities.

## Features
- **High Quality Mirroring:** Uses `adb screenrecord` + H.264 decoding for clear visuals (720x1600 default).
- **Zero Latency (Approx):** Uses PyAV with low-delay flags.
- **Multi-Device Support:** Automatically detects connected devices and lets you choose if more than one is found.
- **Input Control:** 
    - Click to Tap
    - Drag to Swipe
    - Long Press (Hold Click)
    - Scroll Wheel
- **Navigation Bar:** Dedicated Home, Back, and Recents buttons.
- **Keyboard Support:** Type on your PC keyboard to send text to Android (supports Greek/Unicode).
- **Clipboard Paste:** Press `Ctrl+V` to paste text from your PC to the Android device.
- **File Transfer:** Drag & Drop files onto the window to push them to `/sdcard/Download/` and open them immediately.

## Requirements
- Python 3.8+
- Android Device with USB Debugging Enabled

## Installation
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Ensure `adb` drivers are installed for your phone.

## Usage
Run the main script:
```bash
python main.py
```
- If one device is connected, it auto-connects.
- If multiple devices are connected, a selection dialog appears.

## Troubleshooting
- **Black Screen?** Some devices (Xiaomi/Redmi) need "USB Debugging (Security Settings)" enabled to allow screen recording/input.
- **Input Lag?** `adb shell input` is inherently slow. This tool uses a threaded queue to prevent app freezing.
