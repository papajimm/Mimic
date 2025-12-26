print("1. Importing sys, os, subprocess, time, threading...")
import sys, os, subprocess, time, threading
print("   Success.")

print("2. Importing numpy...")
try:
    import numpy as np
    print("   Success (numpy).")
except Exception as e:
    print(f"   Failed: {e}")

print("3. Importing av (PyAV)...")
try:
    import av
    print("   Success (av).")
except Exception as e:
    print(f"   Failed: {e}")

print("4. Importing PyQt6...")
try:
    from PyQt6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout
    print("   Success (PyQt6).")
except Exception as e:
    print(f"   Failed: {e}")

print("Diagnostic complete.")
