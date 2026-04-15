import sys
import json
import asyncio
import threading
import math
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider, QComboBox)
from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPen
from PySide6.QtCore import Qt, Signal, Slot, QRect

import websockets

class FrequencyRuler(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(30)
        self.start_f = 88.0
        self.stop_f = 108.0
        self.margin = 40 # Utrymme på sidorna för labels

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = start_mhz
        self.stop_f = stop_mhz
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        
        width = self.width() - (2 * self.margin)
        painter.setPen(QPen(Qt.white, 1))
        painter.setFont(QFont("Arial", 8))

        # Beräkna vilka hela MHz som finns inom intervallet
        first_mhz = math.ceil(self.start_f)
        last_mhz = math.floor(self.stop_f)
        
        f_range = self.stop_f - self.start_f
        if f_range <= 0: return

        # Rita markeringar för varje hel MHz
        for mhz in range(first_mhz, last_mhz + 1):
            # Beräkna position relativt start/stopp
            relative_pos = (mhz - self.start_f) / f_range
            x = int(self.margin + (relative_pos * width))
            
            # Rita linje
            painter.drawLine(x, 20, x, 30)
            
            # Rita text
            text = f"{mhz}"
            rect = QRect(x - 20, 0, 40, 20)
            painter.drawText(rect, Qt.AlignCenter, text)
            
        # Rita små markeringar för start och stopp vid kanterna om de inte är hela MHz
        painter.setPen(QPen(QColor(150, 150, 150), 1))
        painter.drawLine(self.margin, 25, self.margin, 30)
        painter.drawLine(self.width() - self.margin, 25, self.width() - self.margin, 30)

class WaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(500)
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0
        self.margin = 40 # Måste matcha FrequencyRuler

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
                # Snyggare färgskala
                self.image.setPixelColor(x, self.current_row, QColor(norm_val, int(norm_val*0.3), 255-norm_val))

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Rita vattenfallet inom marginalerna
        display_rect = QRect(self.margin, 0, self.width() - 2 * self.margin, self.height())
        painter.drawImage(display_rect, self.image)
        
        # Rita en ram runt vattenfallet
        painter.setPen(QColor(60, 60, 60))
        painter.drawRect(display_rect)

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SDR Professional Visualizer")
        self.resize(1300, 850)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 10, 0, 10)
        main_layout.setSpacing(0)

        # --- KONTROLLRAD ---
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(50, 0, 50, 10) # Matcha marginalen ungefär
        
        self.start_freq = QLineEdit("88.0")
        self.stop_freq = QLineEdit("108.0")
        for e in [self.start_freq, self.stop_freq]: e.setFixedWidth(70)
        
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["256","512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText("1024")
        
        self.step_input = QLineEdit("1.5")
        self.step_input.setFixedWidth(40)
        
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 255); self.threshold_slider.setValue(45)
        self.threshold_slider.setFixedWidth(150)
        self.threshold_val_label = QLabel("45")
        self.threshold_val_label.setFixedWidth(30)
        self.threshold_slider.valueChanged.connect(lambda v: self.threshold_val_label.setText(str(v)))

        self.btn_update = QPushButton("Svep")
        self.btn_update.setStyleSheet("background-color: #0078d7; font-weight: bold; padding: 5px 15px;")

        ctrl_layout.addWidget(QLabel("Start (MHz):"))
        ctrl_layout.addWidget(self.start_freq)
        ctrl_layout.addWidget(QLabel("Stopp:"))
        ctrl_layout.addWidget(self.stop_freq)
        ctrl_layout.addWidget(QLabel("FFT:"))
        ctrl_layout.addWidget(self.fft_combo)
        ctrl_layout.addWidget(QLabel("Steg:"))
        ctrl_layout.addWidget(self.step_input)
        ctrl_layout.addSpacing(20)
        ctrl_layout.addWidget(QLabel("Brus:"))
        ctrl_layout.addWidget(self.threshold_slider)
        ctrl_layout.addWidget(self.threshold_val_label)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.btn_update)

        # --- VISUALISERING ---
        self.ruler = FrequencyRuler()
        self.waterfall = WaterfallWidget()
        
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(self.ruler)
        main_layout.addWidget(self.waterfall, 1)
        
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
        try:
            s_mhz = float(self.start_freq.text().replace(',', '.'))
            e_mhz = float(self.stop_freq.text().replace(',', '.'))
            self.ruler.set_range(s_mhz, e_mhz)
            if self.ws:
                msg = json.dumps({
                    "start": s_mhz * 1e6,
                    "stop": e_mhz * 1e6,
                    "fft_size": int(self.fft_combo.currentText()),
                    "step_size": float(self.step_input.text().replace(',', '.')) * 1e6
                })
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as ex: print(ex)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())