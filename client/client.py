import sys
import json
import asyncio
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider, QComboBox)
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import Qt, Signal, Slot
import websockets

class WaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(500)
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0

    def add_line(self, data_bytes, threshold):
        width = len(data_bytes)
        if width == 0: return

        if self.image.width() != width:
            self.image = QImage(width, 1000, QImage.Format_RGB32)
            self.image.fill(Qt.black)
            self.current_row = 0

        for x in range(width):
            val = data_bytes[x]
            if val < threshold:
                self.image.setPixelColor(x, self.current_row, QColor(0, 0, 0))
            else:
                norm_val = int(((val - threshold) / (255 - threshold)) * 255)
                norm_val = max(0, min(255, norm_val))
                # Heatmap: Inferno-style
                self.image.setPixelColor(x, self.current_row, QColor(norm_val, int(norm_val*0.6), int(255-norm_val)))

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(self.rect(), self.image)

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SDR Advanced Scanner")
        self.resize(1300, 850)
        
        main_layout = QVBoxLayout()
        ctrl_layout = QHBoxLayout()
        
        # Frekvens-kontroller
        self.start_freq = QLineEdit("88000000")
        self.stop_freq = QLineEdit("108000000")
        
        # FFT Storlek
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["256", "512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText("1024")
        
        # Stegstorlek (i MHz)
        self.step_input = QLineEdit("1.5")
        
        self.btn_update = QPushButton("Uppdatera Scanner")
        self.btn_update.setStyleSheet("background-color: #0078d7; color: white; padding: 5px;")

        # Layout-bygge
        ctrl_layout.addWidget(QLabel("Start (Hz):"))
        ctrl_layout.addWidget(self.start_freq)
        ctrl_layout.addWidget(QLabel("Stop (Hz):"))
        ctrl_layout.addWidget(self.stop_freq)
        ctrl_layout.addWidget(QLabel("FFT Size:"))
        ctrl_layout.addWidget(self.fft_combo)
        ctrl_layout.addWidget(QLabel("Step (MHz):"))
        ctrl_layout.addWidget(self.step_input)
        ctrl_layout.addWidget(self.btn_update)

        # Brusreglage
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 200)
        self.threshold_slider.setValue(50)
        
        self.waterfall = WaterfallWidget()
        
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(QLabel("Bruströskel:"))
        main_layout.addWidget(self.threshold_slider)
        main_layout.addWidget(self.waterfall)
        
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        
        self.btn_update.clicked.connect(self.send_settings)
        self.data_received.connect(lambda d: self.waterfall.add_line(d, self.threshold_slider.value()))
        
        self.loop = asyncio.new_event_loop()
        self.ws = None
        threading.Thread(target=self.start_async_loop, daemon=True).start()

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_handler())

    async def network_handler(self):
        while True:
            try:
                async with websockets.connect("ws://192.168.128.82:8765") as ws:
                    self.ws = ws
                    while True:
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
            except:
                await asyncio.sleep(2)

    def send_settings(self):
        if self.ws:
            try:
                msg = json.dumps({
                    "start": float(self.start_freq.text()),
                    "stop": float(self.stop_freq.text()),
                    "fft_size": int(self.fft_combo.currentText()),
                    "step_size": float(self.step_input.text()) * 1e6
                })
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
            except Exception as e:
                print(f"Input fel: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())