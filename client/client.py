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
        self.setFixedHeight(35)
        self.start_f = 88.0
        self.stop_f = 108.0
        self.margin = 40 

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = start_mhz
        self.stop_f = stop_mhz
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        
        width_draw_area = self.width() - (2 * self.margin)
        if width_draw_area <= 0: return
        
        painter.setPen(QPen(Qt.white, 1))
        painter.setFont(QFont("Arial", 9))

        num_ticks = 10
        f_range = self.stop_f - self.start_f
        
        for i in range(num_ticks):
            fraction = i / (num_ticks - 1)
            freq_mhz = self.start_f + (fraction * f_range)
            x = int(self.margin + (fraction * width_draw_area))
            
            painter.drawLine(x, 22, x, 32)
            text = f"{freq_mhz:.1f}" if f_range < 50 else f"{int(freq_mhz)}"
            rect = QRect(x - 25, 2, 50, 20)
            painter.drawText(rect, Qt.AlignCenter, text)

class WaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(400)
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0
        self.margin = 40
        self.color_lut = [self._rainbow_color(i / 255.0) for i in range(256)]

    @staticmethod
    def _rainbow_color(norm):
        # Standardiserad "regnbåge": låg intensitet = blå, hög = röd.
        hue = (2.0 / 3.0) * (1.0 - max(0.0, min(1.0, norm)))
        return QColor.fromHsvF(hue, 1.0, 1.0)

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
                # Skalning för heatmap
                nv = int(((val - threshold) / (255 - threshold)) * 255)
                nv = max(0, min(255, nv))
                self.image.setPixelColor(x, self.current_row, self.color_lut[nv])

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Rita vattenfallet centrerat mellan marginalerna
        display_rect = QRect(self.margin, 0, self.width() - 2 * self.margin, self.height())
        painter.drawImage(display_rect, self.image)
        # Ram
        painter.setPen(QColor(70, 70, 70))
        painter.drawRect(display_rect)

class SpectrumLineWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(110)
        self.margin = 40
        self.start_f = 88.0
        self.stop_f = 108.0
        self.latest_data = b""

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = start_mhz
        self.stop_f = stop_mhz
        self.update()

    def set_data(self, data_bytes):
        self.latest_data = data_bytes
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        draw_rect = QRect(self.margin, 6, self.width() - 2 * self.margin, self.height() - 12)
        if draw_rect.width() <= 2 or draw_rect.height() <= 2:
            return

        painter.setPen(QColor(55, 55, 55))
        painter.drawRect(draw_rect)

        # Horisontella referenslinjer för signalstyrka.
        painter.setPen(QPen(QColor(45, 45, 45), 1, Qt.DashLine))
        for frac in [0.25, 0.5, 0.75]:
            y = int(draw_rect.top() + frac * draw_rect.height())
            painter.drawLine(draw_rect.left(), y, draw_rect.right(), y)

        if not self.latest_data:
            return

        width = len(self.latest_data)
        if width <= 1:
            return

        x_step = draw_rect.width() / (width - 1)
        points = []
        for i, val in enumerate(self.latest_data):
            x = int(draw_rect.left() + i * x_step)
            # 255 = starkast -> högst upp i ritytan.
            norm = max(0.0, min(1.0, val / 255.0))
            y = int(draw_rect.bottom() - norm * draw_rect.height())
            points.append((x, y))

        painter.setPen(QPen(QColor(0, 235, 255), 2))
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            painter.drawLine(p0[0], p0[1], p1[0], p1[1])

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gemini SDR Visualizer")
        self.resize(1200, 800)
        self.setStyleSheet("background-color: #121212; color: #e0e0e0;")
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 10, 0, 0)
        main_layout.setSpacing(0)

        # --- KONTROLLRAD ---
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(15, 0, 15, 10)
        
        self.start_input = QLineEdit("88.0")
        self.stop_input = QLineEdit("108.0")
        for inp in [self.start_input, self.stop_input]: inp.setFixedWidth(60)
        
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["256","512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText("1024")
        
        self.step_input = QLineEdit("1.5")
        self.step_input.setFixedWidth(40)
        
        self.thresh_slider = QSlider(Qt.Horizontal)
        self.thresh_slider.setRange(0, 255)
        self.thresh_slider.setValue(45)
        self.thresh_slider.setFixedWidth(120)
        self.thresh_label = QLabel("45")
        self.thresh_label.setFixedWidth(25)
        self.thresh_slider.valueChanged.connect(lambda v: self.thresh_label.setText(str(v)))

        self.btn_run = QPushButton("SVEP")
        self.btn_run.setStyleSheet("background-color: #0063b1; font-weight: bold; padding: 5px 15px; border-radius: 3px;")
        self.btn_run.clicked.connect(self.send_settings)

        ctrl_layout.addWidget(QLabel("Start (MHz):"))
        ctrl_layout.addWidget(self.start_input)
        ctrl_layout.addWidget(QLabel("Stopp:"))
        ctrl_layout.addWidget(self.stop_input)
        ctrl_layout.addWidget(QLabel("FFT:"))
        ctrl_layout.addWidget(self.fft_combo)
        ctrl_layout.addWidget(QLabel("Steg:"))
        ctrl_layout.addWidget(self.step_input)
        ctrl_layout.addSpacing(20)
        ctrl_layout.addWidget(QLabel("Brus:"))
        ctrl_layout.addWidget(self.thresh_slider)
        ctrl_layout.addWidget(self.thresh_label)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.btn_run)

        # --- VISUALISERING ---
        self.spectrum_line = SpectrumLineWidget()
        self.ruler = FrequencyRuler()
        self.waterfall = WaterfallWidget()
        
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(self.spectrum_line)
        main_layout.addWidget(self.ruler)
        main_layout.addWidget(self.waterfall, 1)
        
        # Nätverk
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.data_received.connect(self.on_data_received)
        threading.Thread(target=self.start_async, daemon=True).start()

    @Slot(bytes)
    def on_data_received(self, data):
        self.spectrum_line.set_data(data)
        self.waterfall.add_line(data, self.thresh_slider.value())

    def start_async(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_worker())

    async def network_worker(self):
        while True:
            try:
                # Ändra 'localhost' till serverns IP om den körs på annan maskin
                async with websockets.connect("ws://127.0.0.1:8765") as ws:
                    self.ws = ws
                    while True:
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
            except:
                await asyncio.sleep(2)

    def send_settings(self):
        try:
            s = float(self.start_input.text().replace(',', '.'))
            e = float(self.stop_input.text().replace(',', '.'))
            self.ruler.set_range(s, e)
            self.spectrum_line.set_range(s, e)
            if self.ws:
                msg = json.dumps({
                    "start": s * 1e6,
                    "stop": e * 1e6,
                    "fft_size": int(self.fft_combo.currentText()),
                    "step_size": float(self.step_input.text().replace(',', '.')) * 1e6
                })
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as err: print(f"Input Error: {err}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
