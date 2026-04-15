import sys
import json
import asyncio
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider, QComboBox)
from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPen
from PySide6.QtCore import Qt, Signal, Slot, QRect

import websockets

class FrequencyRuler(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(30)
        self.start_f = 88e6
        self.stop_f = 108e6

    def set_range(self, start, stop):
        self.start_f = start
        self.stop_f = stop
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Bakgrund
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        
        width = self.width()
        painter.setPen(QPen(Qt.white, 1))
        painter.setFont(QFont("Arial", 8))

        # Rita 10 markeringar
        num_ticks = 10
        for i in range(num_ticks + 1):
            x = int(i * (width / num_ticks))
            # Linjär interpolering för frekvensvärdet
            freq = self.start_f + (i / num_ticks) * (self.stop_f - self.start_f)
            freq_mhz = freq / 1e6
            
            # Rita vertikal linje
            painter.drawLine(x, 20, x, 30)
            
            # Rita text (centrerad över linjen, utom vid kanterna)
            text = f"{freq_mhz:.1f} MHz"
            rect = QRect(x - 30, 0, 60, 20)
            painter.drawText(rect, Qt.AlignCenter, text)

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
                # Snygg "Plasma" färgskala
                self.image.setPixelColor(x, self.current_row, QColor(norm_val, int(norm_val*0.4), 255-norm_val))

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Rita bilden så att den expanderar till hela widgetens yta
        painter.drawImage(self.rect(), self.image)

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SDR Advanced Visualizer")
        self.resize(1300, 850)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 5, 0, 0)
        main_layout.setSpacing(2)

        # --- KONTROLLRAD ---
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(10, 0, 10, 0)
        
        self.start_freq = QLineEdit("88000000")
        self.stop_freq = QLineEdit("108000000")
        for e in [self.start_freq, self.stop_freq]: e.setFixedWidth(90)
        
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["128","256","512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText("1024")
        
        self.step_input = QLineEdit("1.5")
        self.step_input.setFixedWidth(40)
        
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 200); self.threshold_slider.setValue(50)
        self.threshold_slider.setFixedWidth(150)

        self.btn_update = QPushButton("Svep")
        self.btn_update.setStyleSheet("background-color: #0078d7; font-weight: bold; padding: 4px;")

        ctrl_layout.addWidget(QLabel("Start (Hz):"))
        ctrl_layout.addWidget(self.start_freq)
        ctrl_layout.addWidget(QLabel("Stopp:"))
        ctrl_layout.addWidget(self.stop_freq)
        ctrl_layout.addWidget(QLabel("FFT:"))
        ctrl_layout.addWidget(self.fft_combo)
        ctrl_layout.addWidget(QLabel("Steg (MHz):"))
        ctrl_layout.addWidget(self.step_input)
        ctrl_layout.addSpacing(15)
        ctrl_layout.addWidget(QLabel("Brus:"))
        ctrl_layout.addWidget(self.threshold_slider)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.btn_update)

        # --- VISUALISERING ---
        self.ruler = FrequencyRuler()
        self.waterfall = WaterfallWidget()
        
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(self.ruler)
        main_layout.addWidget(self.waterfall, 1) # Vattenfallet tar resten
        
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        
        # --- KOPPLINGAR ---
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
            s = float(self.start_freq.text())
            e = float(self.stop_freq.text())
            # Uppdatera linjalen direkt
            self.ruler.set_range(s, e)
            
            if self.ws:
                msg = json.dumps({
                    "start": s, "stop": e,
                    "fft_size": int(self.fft_combo.currentText()),
                    "step_size": float(self.step_input.text()) * 1e6
                })
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as ex: print(ex)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())