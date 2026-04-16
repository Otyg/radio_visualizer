import sys
import json
import asyncio
import threading
import math
import argparse
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider, QComboBox, QCheckBox)
from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPen
from PySide6.QtCore import Qt, Signal, Slot, QRect

import websockets


def normalize_remote_target(remote):
    target = (remote or "").strip()
    if not target:
        target = "localhost"
    if "://" in target:
        return target
    return f"ws://{target}:8765"

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
        if width == 0:
            return False

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
        wrapped_to_top = self.current_row == 0
        self.update()
        return wrapped_to_top

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
        self.setFixedHeight(170)
        self.margin = 40
        self.start_f = 88.0
        self.stop_f = 108.0
        self.latest_data = b""
        self.max_hold_data = None
        self.threshold = 45
        # Kalibrering mot serverns byte-skala: dB = (value / 3) - 60.
        self.noise_reference_db = -30.0
        self.min_display_span_db = 18.0
        self.bottom_padding_db = 12.0
        # Fast relativ skala: när brusnivån ändras flyttas hela grafen.
        # Exempel: brus -30 dB => topp +30 dB (60 dB över brusref).
        self.noise_to_top_db = 60.0

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = start_mhz
        self.stop_f = stop_mhz
        self.update()

    def set_data(self, data_bytes):
        self.latest_data = data_bytes
        if not data_bytes:
            self.update()
            return

        if self.max_hold_data is None or len(self.max_hold_data) != len(data_bytes):
            self.max_hold_data = bytearray(data_bytes)
        else:
            for i, value in enumerate(data_bytes):
                if value > self.max_hold_data[i]:
                    self.max_hold_data[i] = value
        self.update()

    def set_threshold(self, threshold):
        self.threshold = max(0, min(255, int(threshold)))
        self.update()

    def reset_max_hold(self):
        self.max_hold_data = None
        self.update()

    def set_noise_reference_db(self, value_db):
        self.noise_reference_db = float(value_db)
        self.update()

    @staticmethod
    def _byte_to_db(value):
        return (float(value) / 3.0) - 60.0

    def _compute_db_window(self):
        top_db = self.noise_reference_db + self.noise_to_top_db
        noise_floor_db = min(self.noise_reference_db, top_db - 4.0)
        bottom_db = noise_floor_db - self.bottom_padding_db
        span_db = top_db - bottom_db
        if span_db < self.min_display_span_db:
            bottom_db = top_db - self.min_display_span_db
        return bottom_db, top_db

    @staticmethod
    def _db_to_y(db_value, bottom_db, top_db, draw_rect):
        span_db = max(1e-6, top_db - bottom_db)
        norm = (db_value - bottom_db) / span_db
        norm = max(0.0, min(1.0, norm))
        usable_height = max(1, draw_rect.height() - 1)
        y = int(round(draw_rect.bottom() - norm * usable_height))
        return max(draw_rect.top() + 1, min(draw_rect.bottom() - 1, y))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        draw_rect = QRect(self.margin, 8, self.width() - 2 * self.margin, self.height() - 16)
        if draw_rect.width() <= 2 or draw_rect.height() <= 2:
            return

        painter.setPen(QColor(55, 55, 55))
        painter.drawRect(draw_rect)

        # Horisontella referenslinjer för signalstyrka.
        painter.setPen(QPen(QColor(45, 45, 45), 1, Qt.DashLine))
        for frac in [0.25, 0.5, 0.75]:
            y = int(draw_rect.top() + frac * draw_rect.height())
            painter.drawLine(draw_rect.left(), y, draw_rect.right(), y)

        bottom_db, top_db = self._compute_db_window()

        if not self.latest_data:
            threshold_db = self._byte_to_db(self.threshold)
            y_thr = self._db_to_y(threshold_db, bottom_db, top_db, draw_rect)
            painter.setPen(QPen(QColor(255, 60, 60), 1.5))
            painter.drawLine(draw_rect.left(), y_thr, draw_rect.right(), y_thr)
            return

        width = len(self.latest_data)
        if width <= 1:
            return

        x_step = draw_rect.width() / (width - 1)
        points = []
        for i, val in enumerate(self.latest_data):
            x = int(draw_rect.left() + i * x_step)
            y = self._db_to_y(self._byte_to_db(val), bottom_db, top_db, draw_rect)
            points.append((x, y))

        if self.max_hold_data and len(self.max_hold_data) == width:
            max_points = []
            for i, val in enumerate(self.max_hold_data):
                x = int(draw_rect.left() + i * x_step)
                y = self._db_to_y(self._byte_to_db(val), bottom_db, top_db, draw_rect)
                max_points.append((x, y))

            painter.setPen(QPen(QColor(0, 95, 150), 1.5, Qt.DashLine))
            for i in range(len(max_points) - 1):
                p0 = max_points[i]
                p1 = max_points[i + 1]
                painter.drawLine(p0[0], p0[1], p1[0], p1[1])

        painter.setPen(QPen(QColor(0, 235, 255), 2))
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            painter.drawLine(p0[0], p0[1], p1[0], p1[1])

        threshold_db = self._byte_to_db(self.threshold)
        y_thr = self._db_to_y(threshold_db, bottom_db, top_db, draw_rect)
        painter.setPen(QPen(QColor(255, 60, 60), 1.5))
        painter.drawLine(draw_rect.left(), y_thr, draw_rect.right(), y_thr)

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self, remote_url, start_mhz, stop_mhz, step_mhz, fft_size):
        super().__init__()
        self.remote_url = remote_url
        self.setWindowTitle("Gemini SDR Visualizer")
        self.resize(1200, 800)
        self.setStyleSheet("background-color: #121212; color: #e0e0e0;")
        self.auto_noise_enabled = False
        self.auto_noise_estimate = None
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 10, 0, 0)
        main_layout.setSpacing(0)

        # --- KONTROLLRAD ---
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(15, 0, 15, 10)
        
        self.start_input = QLineEdit(f"{start_mhz:g}")
        self.stop_input = QLineEdit(f"{stop_mhz:g}")
        for inp in [self.start_input, self.stop_input]: inp.setFixedWidth(60)
        
        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["256","512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText(str(fft_size))
        
        self.step_input = QLineEdit(f"{step_mhz:g}")
        self.step_input.setFixedWidth(40)
        
        self.thresh_slider = QSlider(Qt.Horizontal)
        self.thresh_slider.setRange(0, 255)
        self.thresh_slider.setValue(45)
        self.thresh_slider.setFixedWidth(120)
        self.thresh_label = QLabel("45")
        self.thresh_label.setFixedWidth(25)
        self.thresh_slider.valueChanged.connect(self.on_threshold_changed)
        self.chk_auto_noise = QCheckBox("Auto brus")
        self.chk_auto_noise.setChecked(False)
        self.chk_auto_noise.toggled.connect(self.on_toggle_auto_noise)

        self.btn_run = QPushButton("SVEP")
        self.btn_run.setStyleSheet("background-color: #0063b1; font-weight: bold; padding: 5px 15px; border-radius: 3px;")
        self.btn_run.clicked.connect(self.send_settings)

        self.chk_spectrum = QCheckBox("Linjespektrum")
        self.chk_spectrum.setChecked(True)
        self.chk_spectrum.toggled.connect(self.on_toggle_spectrum)

        self.chk_waterfall = QCheckBox("Vattenfall")
        self.chk_waterfall.setChecked(True)
        self.chk_waterfall.toggled.connect(self.on_toggle_waterfall)

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
        ctrl_layout.addWidget(self.chk_auto_noise)
        ctrl_layout.addSpacing(12)
        ctrl_layout.addWidget(self.chk_spectrum)
        ctrl_layout.addWidget(self.chk_waterfall)
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
        self.on_threshold_changed(self.thresh_slider.value())
        self.ruler.set_range(start_mhz, stop_mhz)
        self.spectrum_line.set_range(start_mhz, stop_mhz)

    @Slot(int)
    def on_threshold_changed(self, value):
        self.thresh_label.setText(str(value))
        self.spectrum_line.set_threshold(value)
        # Samma slider som maskar vattenfallet sätter även nollnivån i linjespektrumet.
        self.spectrum_line.set_noise_reference_db((float(value) / 3.0) - 60.0)

    @Slot(bool)
    def on_toggle_auto_noise(self, enabled):
        self.auto_noise_enabled = bool(enabled)
        self.auto_noise_estimate = None

    @staticmethod
    def _percentile_from_hist(hist, total, percentile):
        if total <= 0:
            return 0
        target = int(round((percentile / 100.0) * (total - 1)))
        running = 0
        for value, count in enumerate(hist):
            running += count
            if running > target:
                return value
        return 255

    def _update_auto_noise_threshold(self, data):
        if not data:
            return

        hist = [0] * 256
        for value in data:
            hist[value] += 1
        total = len(data)

        p50 = self._percentile_from_hist(hist, total, 50)
        p90 = self._percentile_from_hist(hist, total, 90)
        spread = max(1, p90 - p50)

        # Gissa brusgolv via robusta percentiler och håll tröskeln tydligare ovanför golvet.
        raw_target = p50 + int(0.45 * spread) + 10
        raw_target = max(0, min(255, raw_target))

        if self.auto_noise_estimate is None:
            self.auto_noise_estimate = float(raw_target)
        else:
            self.auto_noise_estimate = 0.70 * self.auto_noise_estimate + 0.30 * raw_target

        self.thresh_slider.setValue(int(round(self.auto_noise_estimate)))

    @Slot(bytes)
    def on_data_received(self, data):
        if self.auto_noise_enabled:
            self._update_auto_noise_threshold(data)
        if self.chk_spectrum.isChecked():
            self.spectrum_line.set_data(data)
        if self.chk_waterfall.isChecked():
            wrapped = self.waterfall.add_line(data, self.thresh_slider.value())
            if wrapped:
                self.spectrum_line.reset_max_hold()

    @Slot(bool)
    def on_toggle_spectrum(self, enabled):
        self.spectrum_line.setVisible(enabled)

    @Slot(bool)
    def on_toggle_waterfall(self, enabled):
        self.waterfall.setVisible(enabled)

    def _build_settings_payload(self):
        s = float(self.start_input.text().replace(',', '.'))
        e = float(self.stop_input.text().replace(',', '.'))
        return {
            "start": s * 1e6,
            "stop": e * 1e6,
            "fft_size": int(self.fft_combo.currentText()),
            "step_size": float(self.step_input.text().replace(',', '.')) * 1e6
        }

    def start_async(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_worker())

    async def network_worker(self):
        while True:
            try:
                async with websockets.connect(self.remote_url) as ws:
                    self.ws = ws
                    try:
                        # Skicka alltid startkonfig vid anslutning (inklusive default/CLI-värden).
                        await ws.send(json.dumps(self._build_settings_payload()))
                    except Exception as err:
                        print(f"Init send error: {err}")
                    while True:
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
            except:
                await asyncio.sleep(2)

    def send_settings(self):
        try:
            payload = self._build_settings_payload()
            s = payload["start"] / 1e6
            e = payload["stop"] / 1e6
            self.ruler.set_range(s, e)
            self.spectrum_line.set_range(s, e)
            if self.ws:
                msg = json.dumps(payload)
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as err: print(f"Input Error: {err}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDR-klient")
    parser.add_argument(
        "--remote",
        default="localhost",
        help="Serverns hostname eller IP, t.ex. localhost eller 192.168.1.50",
    )
    parser.add_argument("--start", type=float, default=88.0, help="Startfrekvens i MHz")
    parser.add_argument("--stop", type=float, default=108.0, help="Stoppfrekvens i MHz")
    parser.add_argument("--step", type=float, default=1.5, help="Stegstorlek i MHz")
    parser.add_argument("--fft", type=int, choices=[256, 512, 1024, 2048, 4096], default=1024, help="FFT-storlek")
    args = parser.parse_args()
    remote_url = normalize_remote_target(args.remote)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow(remote_url, args.start, args.stop, args.step, args.fft)
    win.show()
    sys.exit(app.exec())
