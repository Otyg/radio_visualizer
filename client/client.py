import sys
import json
import asyncio
import threading
import math
import argparse
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider, QComboBox, QCheckBox, QFileDialog, QTabWidget, QMessageBox, QGridLayout, QFrame)
from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPen
from PySide6.QtCore import Qt, Signal, Slot, QRect, QTimer

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
        self.mode = "continuous"
        self.scan_channels = []

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = start_mhz
        self.stop_f = stop_mhz
        self.mode = "continuous"
        self.scan_channels = []
        self.update()

    def set_scan_channels(self, channels):
        active = [dict(ch) for ch in channels if bool(ch.get("active", False))]
        if not active:
            active = [dict(ch) for ch in channels]
        normalized = []
        for idx, ch in enumerate(active):
            try:
                center = float(ch.get("center_mhz"))
                bandwidth = max(0.001, float(ch.get("bandwidth_mhz")))
            except Exception:
                continue
            label = str(ch.get("label", "")).strip() or f"Kanal {idx + 1}"
            normalized.append(
                {
                    "label": label,
                    "center_mhz": center,
                    "bandwidth_mhz": bandwidth,
                }
            )
        if normalized:
            self.mode = "scan_channels"
            self.scan_channels = normalized
            self.start_f = min(ch["center_mhz"] - (ch["bandwidth_mhz"] / 2.0) for ch in normalized)
            self.stop_f = max(ch["center_mhz"] + (ch["bandwidth_mhz"] / 2.0) for ch in normalized)
        else:
            self.mode = "continuous"
            self.scan_channels = []
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        
        width_draw_area = self.width() - (2 * self.margin)
        if width_draw_area <= 0: return
        
        painter.setPen(QPen(Qt.white, 1))
        painter.setFont(QFont("Arial", 9))

        if self.mode == "scan_channels" and self.scan_channels:
            total_bw = sum(ch["bandwidth_mhz"] for ch in self.scan_channels)
            if total_bw <= 0:
                return
            x_cursor = float(self.margin)
            for idx, ch in enumerate(self.scan_channels):
                frac = ch["bandwidth_mhz"] / total_bw
                width_px = width_draw_area * frac
                x0 = int(round(x_cursor))
                x1 = int(round(x_cursor + width_px))
                x_cursor += width_px

                painter.drawLine(x0, 22, x0, 32)
                if idx == len(self.scan_channels) - 1:
                    painter.drawLine(self.margin + width_draw_area, 22, self.margin + width_draw_area, 32)

                label_rect = QRect(x0 + 2, 2, max(1, x1 - x0 - 4), 20)
                painter.drawText(label_rect, Qt.AlignCenter, f"{ch['center_mhz']:.3f}")
            return

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
    frequency_selected = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(400)
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0
        self.margin = 40
        self.start_f = 88.0
        self.stop_f = 108.0
        self.view_mode = "continuous"
        self.scan_centers = []
        self.scan_bandwidth = 0.0
        self.color_lut = [self._rainbow_color(i / 255.0) for i in range(256)]

    @staticmethod
    def _rainbow_color(norm):
        # Standardiserad "regnbåge": låg intensitet = blå, hög = röd.
        hue = (2.0 / 3.0) * (1.0 - max(0.0, min(1.0, norm)))
        return QColor.fromHsvF(hue, 1.0, 1.0)

    def set_range(self, start_mhz, stop_mhz):
        self.start_f = float(start_mhz)
        self.stop_f = float(stop_mhz)
        self.view_mode = "continuous"
        self.scan_centers = []
        self.scan_bandwidth = 0.0
        self.update()

    def set_scan_columns(self, centers_mhz, bandwidth_mhz):
        centers = [float(v) for v in centers_mhz]
        if not centers:
            self.set_range(self.start_f, self.stop_f)
            return
        bw = max(0.001, float(bandwidth_mhz))
        self.view_mode = "column_scan"
        self.scan_centers = centers
        self.scan_bandwidth = bw
        half_bw = bw / 2.0
        self.start_f = min(centers) - half_bw
        self.stop_f = max(centers) + half_bw
        self.update()

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

    def snapshot_image(self):
        if self.image.isNull():
            return QImage()
        if self.current_row == 0:
            return self.image.copy()

        width = self.image.width()
        height = self.image.height()
        ordered = QImage(width, height, QImage.Format_RGB32)
        ordered.fill(Qt.black)
        painter = QPainter(ordered)
        try:
            bottom_height = height - self.current_row
            painter.drawImage(QRect(0, 0, width, bottom_height), self.image, QRect(0, self.current_row, width, bottom_height))
            painter.drawImage(QRect(0, bottom_height, width, self.current_row), self.image, QRect(0, 0, width, self.current_row))
        finally:
            painter.end()
        return ordered

    def paintEvent(self, event):
        painter = QPainter(self)
        # Rita vattenfallet centrerat mellan marginalerna
        display_rect = QRect(self.margin, 0, self.width() - 2 * self.margin, self.height())
        painter.drawImage(display_rect, self.image)
        if self.view_mode == "column_scan" and len(self.scan_centers) > 1 and display_rect.width() > 1:
            columns = len(self.scan_centers)
            sep_pen = QPen(QColor(140, 140, 140), 1)
            sep_pen.setStyle(Qt.DashLine)
            painter.setPen(sep_pen)
            for idx in range(1, columns):
                x = int(display_rect.left() + (idx / columns) * display_rect.width())
                painter.drawLine(x, display_rect.top(), x, display_rect.bottom())
        if self.view_mode == "column_scan" and self.scan_centers and display_rect.width() > 1:
            columns = len(self.scan_centers)
            painter.setFont(QFont("Arial", 8))
            painter.setPen(QColor(230, 230, 230))
            for idx, center in enumerate(self.scan_centers):
                x0 = int(display_rect.left() + (idx / columns) * display_rect.width())
                x1 = int(display_rect.left() + ((idx + 1) / columns) * display_rect.width())
                label_rect = QRect(x0 + 2, display_rect.top() + 2, max(1, x1 - x0 - 4), 16)
                painter.drawText(label_rect, Qt.AlignCenter, f"{center:.3f} MHz")
        # Ram
        painter.setPen(QColor(70, 70, 70))
        painter.drawRect(display_rect)

    def mousePressEvent(self, event):
        display_rect = QRect(self.margin, 0, self.width() - 2 * self.margin, self.height())
        if display_rect.width() <= 0 or not display_rect.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return

        rel_x = (event.position().x() - display_rect.left()) / max(1.0, float(display_rect.width()))
        rel_x = max(0.0, min(1.0, rel_x))
        if self.view_mode == "column_scan" and self.scan_centers:
            columns = len(self.scan_centers)
            bw = max(0.001, float(self.scan_bandwidth))
            col_float = rel_x * columns
            col_idx = min(columns - 1, int(col_float))
            rel_in_col = col_float - col_idx
            half_bw = bw / 2.0
            center = self.scan_centers[col_idx]
            freq_mhz = (center - half_bw) + rel_in_col * bw
        else:
            freq_mhz = self.start_f + rel_x * (self.stop_f - self.start_f)
        self.frequency_selected.emit(freq_mhz)
        super().mousePressEvent(event)


class MiniWaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(110)
        self.image = QImage(256, 300, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0
        self.color_lut = [WaterfallWidget._rainbow_color(i / 255.0) for i in range(256)]

    def clear(self):
        self.image.fill(Qt.black)
        self.current_row = 0
        self.update()

    def add_line(self, data_bytes, threshold):
        width = len(data_bytes)
        if width <= 0:
            return
        if self.image.width() != width:
            self.image = QImage(width, 300, QImage.Format_RGB32)
            self.image.fill(Qt.black)
            self.current_row = 0

        thr = max(0, min(255, int(threshold)))
        for x in range(width):
            val = data_bytes[x]
            if val < thr:
                self.image.setPixelColor(x, self.current_row, QColor(0, 0, 0))
            else:
                nv = int(((val - thr) / max(1, (255 - thr))) * 255)
                nv = max(0, min(255, nv))
                self.image.setPixelColor(x, self.current_row, self.color_lut[nv])

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(14, 14, 14))
        draw_rect = self.rect().adjusted(1, 1, -1, -1)
        if draw_rect.width() <= 1 or draw_rect.height() <= 1:
            return
        painter.drawImage(draw_rect, self.image)
        painter.setPen(QColor(70, 70, 70))
        painter.drawRect(draw_rect)


class ScanChannelTile(QFrame):
    def __init__(self, index, default_freq_mhz, default_bandwidth_mhz):
        super().__init__()
        self.index = int(index)
        self.auto_noise_estimate = None
        self.is_scanning_now = False
        self.is_locked_now = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame { background-color: #1b1b1b; border: 1px solid #3b3b3b; border-radius: 4px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(QLabel("Namn:"))
        self.name_input = QLineEdit(f"Kanal {self.index + 1}")
        self.name_input.setPlaceholderText("Kanalnamn")
        name_row.addWidget(self.name_input, 1)
        layout.addLayout(name_row)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel("MHz:"))
        self.freq_input = QLineEdit(f"{float(default_freq_mhz):g}")
        self.freq_input.setFixedWidth(70)
        row1.addWidget(self.freq_input)
        row1.addWidget(QLabel("BW:"))
        self.bandwidth_input = QLineEdit(f"{float(default_bandwidth_mhz):g}")
        self.bandwidth_input.setFixedWidth(62)
        row1.addWidget(self.bandwidth_input)
        row1.addStretch()
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.active_check = QCheckBox("Aktiv")
        self.active_check.setChecked(self.index == 0)
        row2.addWidget(self.active_check)
        self.auto_noise_check = QCheckBox("Auto brus")
        self.auto_noise_check.setChecked(False)
        self.auto_noise_check.toggled.connect(self._on_toggle_auto_noise)
        row2.addWidget(self.auto_noise_check)
        row2.addWidget(QLabel("Brus dB:"))
        self.noise_db_input = QLineEdit("-35")
        self.noise_db_input.setFixedWidth(56)
        row2.addWidget(self.noise_db_input)
        row2.addWidget(QLabel("Status:"))
        self.scan_status_lamp = QLabel("")
        self.scan_status_lamp.setFixedSize(12, 12)
        row2.addWidget(self.scan_status_lamp)
        row2.addStretch()
        layout.addLayout(row2)

        self.waterfall = MiniWaterfallWidget()
        layout.addWidget(self.waterfall, 1)
        self.set_scan_status(False, False)

    def _on_toggle_auto_noise(self, enabled):
        if enabled:
            self.auto_noise_estimate = None

    @staticmethod
    def _parse_float(text, fallback):
        try:
            return float(str(text).replace(",", "."))
        except Exception:
            return float(fallback)

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

    def _noise_db_to_threshold(self):
        noise_db = self._parse_float(self.noise_db_input.text(), -35.0)
        return int(max(0, min(255, round((noise_db + 60.0) * 3.0))))

    def _estimate_auto_threshold(self, data):
        hist = [0] * 256
        for value in data:
            hist[value] += 1
        total = len(data)
        p50 = self._percentile_from_hist(hist, total, 50)
        p90 = self._percentile_from_hist(hist, total, 90)
        spread = max(1, p90 - p50)
        raw_target = p50 + int(0.45 * spread) + 10
        raw_target = max(0, min(255, raw_target))
        if self.auto_noise_estimate is None:
            self.auto_noise_estimate = float(raw_target)
        else:
            self.auto_noise_estimate = 0.70 * self.auto_noise_estimate + 0.30 * raw_target
        return int(round(self.auto_noise_estimate))

    @staticmethod
    def _threshold_to_noise_db(threshold):
        return (float(threshold) / 3.0) - 60.0

    def _current_threshold(self, data):
        if self.auto_noise_check.isChecked():
            threshold = self._estimate_auto_threshold(data)
            noise_db = self._threshold_to_noise_db(threshold)
            self.noise_db_input.setText(f"{noise_db:.1f}")
            return threshold
        return self._noise_db_to_threshold()

    def detect_signal(self, data):
        if not data:
            return False

        threshold = self._current_threshold(data)
        hist = [0] * 256
        for value in data:
            hist[value] += 1
        total = len(data)
        p50 = self._percentile_from_hist(hist, total, 50)
        p90 = self._percentile_from_hist(hist, total, 90)
        p99 = self._percentile_from_hist(hist, total, 99)

        strong_bins = 0
        elevated_limit = min(255, threshold + 6)
        for value in data:
            if value >= elevated_limit:
                strong_bins += 1
        strong_ratio = float(strong_bins) / float(total)

        peak_margin = p99 - max(p50, threshold)
        return (
            p99 >= (threshold + 8)
            and p90 >= (threshold + 3)
            and peak_margin >= 10
            and strong_ratio >= 0.015
        )

    def get_config(self):
        freq_mhz = self._parse_float(self.freq_input.text(), 100.0)
        bandwidth_mhz = max(0.001, self._parse_float(self.bandwidth_input.text(), 0.2))
        noise_reduction_db = self._parse_float(self.noise_db_input.text(), -35.0)
        return {
            "label": self.name_input.text().strip() or f"Kanal {self.index + 1}",
            "center_mhz": freq_mhz,
            "bandwidth_mhz": bandwidth_mhz,
            "active": bool(self.active_check.isChecked()),
            "auto_noise": bool(self.auto_noise_check.isChecked()),
            "noise_reduction_db": noise_reduction_db,
        }

    def set_config(self, cfg):
        if not isinstance(cfg, dict):
            return
        label = str(cfg.get("label", "")).strip()
        if label:
            self.name_input.setText(label)
        freq_mhz = self._parse_float(cfg.get("center_mhz", self.freq_input.text()), self.freq_input.text())
        bandwidth_mhz = max(0.001, self._parse_float(cfg.get("bandwidth_mhz", self.bandwidth_input.text()), self.bandwidth_input.text()))
        noise_reduction_db = self._parse_float(cfg.get("noise_reduction_db", self.noise_db_input.text()), self.noise_db_input.text())
        self.freq_input.setText(f"{freq_mhz:g}")
        self.bandwidth_input.setText(f"{bandwidth_mhz:g}")
        self.active_check.setChecked(bool(cfg.get("active", self.active_check.isChecked())))
        self.auto_noise_check.setChecked(bool(cfg.get("auto_noise", self.auto_noise_check.isChecked())))
        self.noise_db_input.setText(f"{noise_reduction_db:g}")

    def consume_spectrum(self, data):
        if not data:
            return
        threshold = self._current_threshold(data)
        self.waterfall.add_line(data, threshold)

    def set_scan_status(self, scanning, locked):
        self.is_scanning_now = bool(scanning)
        self.is_locked_now = bool(locked)
        if self.is_locked_now:
            color = "#ffd24a"
        elif self.is_scanning_now:
            color = "#58d26a"
        else:
            color = "#3e3e3e"
        self.scan_status_lamp.setStyleSheet(
            f"background-color: {color}; border: 1px solid #202020; border-radius: 6px;"
        )

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
        self.max_display_top_db = 30.0

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
        top_db = self.max_display_top_db
        bottom_db = self.noise_reference_db - self.bottom_padding_db
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
        return int(round(draw_rect.bottom() - norm * usable_height))

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

            max_pen = QPen(QColor(0, 95, 150), 1.0, Qt.DashLine)
            max_pen.setCapStyle(Qt.FlatCap)
            painter.setPen(max_pen)
            for i in range(len(max_points) - 1):
                p0 = max_points[i]
                p1 = max_points[i + 1]
                painter.drawLine(p0[0], p0[1], p1[0], p1[1])

        spectrum_pen = QPen(QColor(0, 235, 255), 1.0)
        spectrum_pen.setCapStyle(Qt.FlatCap)
        painter.setPen(spectrum_pen)
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            painter.drawLine(p0[0], p0[1], p1[0], p1[1])

        threshold_db = self._byte_to_db(self.threshold)
        y_thr = self._db_to_y(threshold_db, bottom_db, top_db, draw_rect)
        threshold_pen = QPen(QColor(255, 60, 60), 1.0)
        threshold_pen.setCapStyle(Qt.FlatCap)
        painter.setPen(threshold_pen)
        painter.drawLine(draw_rect.left(), y_thr, draw_rect.right(), y_thr)

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self, remote_url, start_mhz, stop_mhz, step_mhz, fft_size, initial_mode="sweep", center_mhz=None, bandwidth_mhz=None, scan_centers_csv=None, scan_bandwidth_mhz=None, scan_dwell_ms=None):
        super().__init__()
        self.remote_url = remote_url
        self.setWindowTitle("Gemini SDR Visualizer")
        desired_w, desired_h = 1200, 800
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = max(900, int(avail.width() * 0.95))
            max_h = max(650, int(avail.height() * 0.95))
            desired_w = min(desired_w, max_w)
            desired_h = min(desired_h, max_h)
            self.resize(desired_w, desired_h)
            self.move(
                avail.left() + max(0, (avail.width() - desired_w) // 2),
                avail.top() + max(0, (avail.height() - desired_h) // 2),
            )
        else:
            self.resize(desired_w, desired_h)
        self.setStyleSheet("background-color: #121212; color: #e0e0e0;")
        self.auto_noise_enabled = False
        self.auto_noise_estimate = None
        self.is_paused = False
        self.requested_server_shutdown = False
        default_center = (start_mhz + stop_mhz) / 2.0 if center_mhz is None else float(center_mhz)
        default_bandwidth = abs(stop_mhz - start_mhz) if bandwidth_mhz is None else float(bandwidth_mhz)
        if default_bandwidth <= 0:
            default_bandwidth = max(0.1, float(step_mhz))
        default_scan_bandwidth = 0.25 if scan_bandwidth_mhz is None else float(scan_bandwidth_mhz)
        if default_scan_bandwidth <= 0:
            default_scan_bandwidth = 0.25
        default_scan_dwell_ms = 80.0 if scan_dwell_ms is None else float(scan_dwell_ms)
        if default_scan_dwell_ms <= 0:
            default_scan_dwell_ms = 80.0
        self.scan_channel_count = 15
        self.server_sample_rate_hz = 2.4e6
        self.scan_lock_tile_index = None
        self.scan_lock_streak_tile_index = None
        self.scan_lock_hit_count = 0
        self.scan_unlock_miss_count = 0
        self.scan_lock_hit_frames = 2
        self.scan_unlock_miss_frames = 3
        self.runtime_scan_active_indices = None
        
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
        self.step_input = QLineEdit(f"{step_mhz:g}")
        self.center_input = QLineEdit(f"{default_center:g}")
        self.bandwidth_input = QLineEdit(f"{default_bandwidth:g}")
        self.scan_dwell_input = QLineEdit(f"{default_scan_dwell_ms:g}")
        for inp in [self.start_input, self.stop_input, self.center_input, self.bandwidth_input, self.scan_dwell_input]:
            inp.setFixedWidth(65)
        self.step_input.setFixedWidth(55)

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setDocumentMode(True)

        sweep_tab = QWidget()
        sweep_layout = QHBoxLayout(sweep_tab)
        sweep_layout.setContentsMargins(8, 6, 8, 6)
        sweep_layout.addWidget(QLabel("Start (MHz):"))
        sweep_layout.addWidget(self.start_input)
        sweep_layout.addWidget(QLabel("Stopp:"))
        sweep_layout.addWidget(self.stop_input)
        sweep_layout.addWidget(QLabel("Steg:"))
        sweep_layout.addWidget(self.step_input)
        sweep_layout.addStretch()

        fixed_tab = QWidget()
        fixed_layout = QHBoxLayout(fixed_tab)
        fixed_layout.setContentsMargins(8, 6, 8, 6)
        fixed_layout.addWidget(QLabel("Center (MHz):"))
        fixed_layout.addWidget(self.center_input)
        fixed_layout.addWidget(QLabel("Bandbredd (MHz):"))
        fixed_layout.addWidget(self.bandwidth_input)
        fixed_layout.addStretch()

        self.mode_tabs.addTab(sweep_tab, "Svep")
        self.mode_tabs.addTab(fixed_tab, "Fast center")
        scan_tab = QWidget()
        scan_layout = QHBoxLayout(scan_tab)
        scan_layout.setContentsMargins(8, 6, 8, 6)
        scan_layout.addWidget(QLabel("Aktiv tid/frekvens (ms):"))
        scan_layout.addWidget(self.scan_dwell_input)
        self.btn_save_scan = QPushButton("Spara")
        self.btn_load_scan = QPushButton("Ladda")
        self.btn_save_scan.clicked.connect(self.save_scan_profile)
        self.btn_load_scan.clicked.connect(self.load_scan_profile)
        scan_layout.addWidget(self.btn_save_scan)
        scan_layout.addWidget(self.btn_load_scan)
        scan_layout.addStretch()
        self.mode_tabs.addTab(scan_tab, "Scanner")
        self.mode_tabs.currentChanged.connect(self.on_mode_tab_changed)

        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["256","512", "1024", "2048", "4096"])
        self.fft_combo.setCurrentText(str(fft_size))

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

        self.btn_run = QPushButton("KÖR")
        self.btn_run.setStyleSheet("background-color: #0063b1; font-weight: bold; padding: 5px 15px; border-radius: 3px;")
        self.btn_run.clicked.connect(self.send_settings)

        self.btn_pause = QPushButton("PAUS")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setStyleSheet("""
            QPushButton {
                background-color: #5c5c5c;
                border: 1px solid #757575;
                font-weight: bold;
                padding: 5px 12px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #696969;
            }
            QPushButton:pressed {
                background-color: #4a4a4a;
                border: 2px inset #2f2f2f;
                padding-top: 6px;
                padding-left: 13px;
            }
            QPushButton:checked {
                background-color: #3d3d3d;
                border: 2px inset #2a2a2a;
                padding-top: 6px;
                padding-left: 13px;
            }
        """)
        self.btn_pause.toggled.connect(self.on_toggle_pause)

        self.btn_export = QPushButton("Exportera bild")
        self.btn_export.setStyleSheet("background-color: #2d7d46; font-weight: bold; padding: 5px 12px; border-radius: 3px;")
        self.btn_export.clicked.connect(self.export_waterfall)

        self.btn_shutdown_server = QPushButton("Stoppa server")
        self.btn_shutdown_server.setStyleSheet("background-color: #a32626; font-weight: bold; padding: 5px 12px; border-radius: 3px;")
        self.btn_shutdown_server.clicked.connect(self.request_server_shutdown)

        self.chk_spectrum = QCheckBox("Linjespektrum")
        self.chk_spectrum.setChecked(True)
        self.chk_spectrum.toggled.connect(self.on_toggle_spectrum)

        self.chk_waterfall = QCheckBox("Vattenfall")
        self.chk_waterfall.setChecked(True)
        self.chk_waterfall.toggled.connect(self.on_toggle_waterfall)
        self.freq_pick_label = QLabel("Klickfrekvens: -")
        self.freq_pick_label.setFixedWidth(170)

        ctrl_layout.addWidget(self.mode_tabs, 1)
        ctrl_layout.addSpacing(10)
        ctrl_layout.addWidget(QLabel("FFT:"))
        ctrl_layout.addWidget(self.fft_combo)
        ctrl_layout.addSpacing(10)
        ctrl_layout.addWidget(QLabel("Brus:"))
        ctrl_layout.addWidget(self.thresh_slider)
        ctrl_layout.addWidget(self.thresh_label)
        ctrl_layout.addWidget(self.chk_auto_noise)
        ctrl_layout.addSpacing(12)
        ctrl_layout.addWidget(self.chk_spectrum)
        ctrl_layout.addWidget(self.chk_waterfall)
        ctrl_layout.addWidget(self.freq_pick_label)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.btn_export)
        ctrl_layout.addWidget(self.btn_shutdown_server)
        ctrl_layout.addWidget(self.btn_pause)
        ctrl_layout.addWidget(self.btn_run)

        # --- VISUALISERING ---
        self.spectrum_line = SpectrumLineWidget()
        self.ruler = FrequencyRuler()
        self.waterfall = WaterfallWidget()
        self.scanner_grid = QWidget()
        self.scanner_grid_layout = QGridLayout(self.scanner_grid)
        self.scanner_grid_layout.setContentsMargins(4, 4, 4, 4)
        self.scanner_grid_layout.setSpacing(6)
        self.scan_channel_tiles = []
        for idx, freq_mhz in enumerate(self._default_scan_channel_frequencies(start_mhz, stop_mhz, scan_centers_csv)):
            tile = ScanChannelTile(idx, freq_mhz, default_scan_bandwidth)
            self.scan_channel_tiles.append(tile)
            row = idx // 5
            col = idx % 5
            self.scanner_grid_layout.addWidget(tile, row, col)

        viz_container = QWidget()
        viz_layout = QVBoxLayout(viz_container)
        viz_layout.setContentsMargins(0, 0, 0, 0)
        viz_layout.setSpacing(0)
        viz_layout.addWidget(self.spectrum_line)
        viz_layout.addWidget(self.ruler)
        viz_layout.addWidget(self.waterfall, 1)
        viz_layout.addWidget(self.scanner_grid, 1)

        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(viz_container, 1)
        
        # Nätverk
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.data_received.connect(self.on_data_received)
        threading.Thread(target=self.start_async, daemon=True).start()
        self.on_threshold_changed(self.thresh_slider.value())
        mode = str(initial_mode).strip().lower()
        if mode == "fixed":
            self.mode_tabs.setCurrentIndex(1)
        elif mode in ("list_scan", "scan_list", "scanner"):
            self.mode_tabs.setCurrentIndex(2)
        else:
            self.mode_tabs.setCurrentIndex(0)
        self._apply_current_range_to_visuals()
        self.waterfall.frequency_selected.connect(self.on_frequency_selected)
        self._sync_visual_mode()
        QTimer.singleShot(0, self._fit_window_to_screen)

    def _fit_window_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()

        width = min(self.width(), avail.width())
        height = min(self.height(), avail.height())
        self.resize(max(640, width), max(480, height))

        x = min(max(self.x(), avail.left()), avail.right() - self.width() + 1)
        y = min(max(self.y(), avail.top()), avail.bottom() - self.height() + 1)
        self.move(x, y)

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
        if self.is_paused:
            return
        scanner_mode = self._get_current_mode() == "list_scan"
        if self.auto_noise_enabled and not scanner_mode:
            self._update_auto_noise_threshold(data)
        if self.chk_spectrum.isChecked() and not scanner_mode:
            self.spectrum_line.set_data(data)
        if scanner_mode:
            self._process_scanner_frame(data)
            return
        if self.chk_waterfall.isChecked():
            wrapped = self.waterfall.add_line(data, self.thresh_slider.value())
            if wrapped:
                self.spectrum_line.reset_max_hold()

    @Slot(bool)
    def on_toggle_pause(self, paused):
        self.is_paused = bool(paused)
        self.btn_pause.setText("FORTSÄTT" if self.is_paused else "PAUS")
        self._send_pause_state()

    def _send_pause_state(self):
        if not self.ws:
            return
        try:
            msg = json.dumps({"paused": self.is_paused})
            asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as err:
            print(f"Pause send error: {err}")

    def request_server_shutdown(self):
        if not self.ws:
            print("Ingen serveranslutning för shutdown.")
            return
        answer = QMessageBox.question(
            self,
            "Stoppa server",
            "Vill du stoppa servern?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.requested_server_shutdown = True
        self.btn_shutdown_server.setEnabled(False)
        try:
            msg = json.dumps({"shutdown": True})
            asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception as err:
            print(f"Shutdown send error: {err}")

    @Slot(bool)
    def on_toggle_spectrum(self, enabled):
        self.spectrum_line.setVisible(enabled)

    @Slot(bool)
    def on_toggle_waterfall(self, enabled):
        self._sync_visual_mode()

    @Slot(float)
    def on_frequency_selected(self, freq_mhz):
        self.freq_pick_label.setText(f"Klickfrekvens: {freq_mhz:.3f} MHz")

    @Slot(int)
    def on_mode_tab_changed(self, _index):
        self._reset_scan_lock()
        self._sync_visual_mode()
        self._apply_current_range_to_visuals()
        if self.mode_tabs.currentIndex() == 0:
            self.btn_run.setText("SVEP")
        elif self.mode_tabs.currentIndex() == 1:
            self.btn_run.setText("KÖR FAST")
        else:
            self.btn_run.setText("KÖR SCANNER")

    @staticmethod
    def _parse_float(text):
        return float(str(text).replace(',', '.'))

    def _extract_center_values(self, text):
        if text is None:
            return []
        values = []
        for raw in str(text).replace(";", ",").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                values.append(self._parse_float(raw))
            except Exception:
                continue
        return values

    def _default_scan_channel_frequencies(self, start_mhz, stop_mhz, scan_centers_csv):
        parsed = self._extract_center_values(scan_centers_csv)
        if parsed:
            values = parsed[:self.scan_channel_count]
            while len(values) < self.scan_channel_count:
                values.append(values[-1])
            return values
        low = min(float(start_mhz), float(stop_mhz))
        high = max(float(start_mhz), float(stop_mhz))
        if math.isclose(low, high):
            return [low for _ in range(self.scan_channel_count)]
        values = []
        for idx in range(self.scan_channel_count):
            frac = idx / max(1, self.scan_channel_count - 1)
            values.append(low + frac * (high - low))
        return values

    def _collect_scan_channels(self):
        channels = []
        for tile in self.scan_channel_tiles:
            channels.append(tile.get_config())
        return channels

    def save_scan_profile(self):
        payload = {
            "version": 1,
            "dwell_time_ms": max(1.0, self._parse_float(self.scan_dwell_input.text())),
            "channels": self._collect_scan_channels(),
        }
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Spara scannerprofil",
            "scanner_profile.json",
            "JSON-filer (*.json);;Alla filer (*)",
        )
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as err:
            print(f"Kunde inte spara profil: {err}")

    def load_scan_profile(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Ladda scannerprofil",
            "",
            "JSON-filer (*.json);;Alla filer (*)",
        )
        if not filename:
            return
        try:
            with open(filename, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as err:
            print(f"Kunde inte läsa profil: {err}")
            return

        if not isinstance(payload, dict):
            print("Ogiltig profil: JSON-roten måste vara ett objekt.")
            return

        dwell_ms = payload.get("dwell_time_ms")
        if dwell_ms is not None:
            try:
                dwell_ms = max(1.0, self._parse_float(dwell_ms))
                self.scan_dwell_input.setText(f"{dwell_ms:g}")
            except Exception:
                pass

        channels = payload.get("channels")
        if isinstance(channels, list):
            for idx, tile in enumerate(self.scan_channel_tiles):
                if idx < len(channels):
                    tile.set_config(channels[idx])
        self._apply_current_range_to_visuals()
        self._sync_visual_mode()

    def _get_current_mode(self):
        idx = self.mode_tabs.currentIndex()
        if idx == 1:
            return "fixed"
        if idx == 2:
            return "list_scan"
        return "sweep"

    def _get_current_range_mhz(self):
        mode = self._get_current_mode()
        if mode == "fixed":
            center = self._parse_float(self.center_input.text())
            bandwidth = max(0.001, self._parse_float(self.bandwidth_input.text()))
            half_bw = bandwidth / 2.0
            return center - half_bw, center + half_bw
        if mode == "list_scan":
            channels = self._collect_scan_channels()
            active = [ch for ch in channels if ch["active"]]
            selected = active if active else channels
            if not selected:
                raise ValueError("Inga scannerkanaler")
            lo = min(ch["center_mhz"] - (ch["bandwidth_mhz"] / 2.0) for ch in selected)
            hi = max(ch["center_mhz"] + (ch["bandwidth_mhz"] / 2.0) for ch in selected)
            return lo, hi

        start = self._parse_float(self.start_input.text())
        stop = self._parse_float(self.stop_input.text())
        if start <= stop:
            return start, stop
        return stop, start

    def _apply_current_range_to_visuals(self):
        try:
            start_mhz, stop_mhz = self._get_current_range_mhz()
        except Exception:
            return
        if self._get_current_mode() == "list_scan":
            self.ruler.set_scan_channels(self._collect_scan_channels())
        else:
            self.ruler.set_range(start_mhz, stop_mhz)
        self.spectrum_line.set_range(start_mhz, stop_mhz)
        self.waterfall.set_range(start_mhz, stop_mhz)

    def _sync_visual_mode(self):
        scanner_mode = self._get_current_mode() == "list_scan"
        show_waterfall = bool(self.chk_waterfall.isChecked())
        self.waterfall.setVisible(show_waterfall and not scanner_mode)
        self.scanner_grid.setVisible(show_waterfall and scanner_mode)

    def _expected_bins_for_bandwidth_hz(self, bandwidth_hz):
        fft_size = int(self.fft_combo.currentText())
        bw = max(1e3, float(bandwidth_hz))
        keep_ratio = max(1e-3, min(1.0, bw / float(self.server_sample_rate_hz)))
        margin = int((1 - keep_ratio) / 2 * fft_size)
        return max(1, fft_size - (2 * margin))

    def _route_scan_data_to_tiles(self, data):
        if not data:
            return []
        tile_iter = []
        for tile in self.scan_channel_tiles:
            if not tile.active_check.isChecked():
                continue
            if self.runtime_scan_active_indices is not None and tile.index not in self.runtime_scan_active_indices:
                continue
            tile_iter.append(tile)
        if not tile_iter:
            return []
        expected = [
            self._expected_bins_for_bandwidth_hz(
                max(0.001, ScanChannelTile._parse_float(tile.bandwidth_input.text(), 0.2)) * 1e6
            )
            for tile in tile_iter
        ]
        pos = 0
        routed = []
        for idx, tile in enumerate(tile_iter):
            bins = expected[idx]
            if idx == len(tile_iter) - 1:
                segment = data[pos:]
            else:
                segment = data[pos:pos + bins]
            pos += bins
            if segment:
                routed.append((tile, segment))
        return routed

    def _send_scan_channels_to_server(self, active_indices):
        if not self.ws:
            return
        active_idx_set = {int(idx) for idx in active_indices}
        self.runtime_scan_active_indices = set(active_idx_set)
        channels = self._collect_scan_channels()
        payload_channels = []
        for idx, ch in enumerate(channels):
            enabled = bool(ch["active"]) and (idx in active_idx_set)
            payload_channels.append(
                {
                    "center": ch["center_mhz"] * 1e6,
                    "bandwidth": ch["bandwidth_mhz"] * 1e6,
                    "active": enabled,
                    "auto_noise": ch["auto_noise"],
                    "noise_reduction_db": ch["noise_reduction_db"],
                }
            )
        dwell_ms = max(1.0, self._parse_float(self.scan_dwell_input.text()))
        payload = {
            "mode": "list_scan",
            "fft_size": int(self.fft_combo.currentText()),
            "dwell_time": dwell_ms / 1000.0,
            "scan_channels": payload_channels,
        }
        msg = json.dumps(payload)
        asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        self._send_pause_state()

    def _lock_scanner_to_tile(self, tile):
        if tile is None:
            return
        self.scan_lock_tile_index = tile.index
        self.scan_unlock_miss_count = 0
        self._send_scan_channels_to_server({tile.index})

    def _unlock_scanner(self):
        self.scan_lock_tile_index = None
        self.scan_unlock_miss_count = 0
        active_indices = {tile.index for tile in self.scan_channel_tiles if tile.active_check.isChecked()}
        self._send_scan_channels_to_server(active_indices)

    def _reset_scan_lock(self):
        self.scan_lock_tile_index = None
        self.scan_lock_streak_tile_index = None
        self.scan_lock_hit_count = 0
        self.scan_unlock_miss_count = 0
        self.runtime_scan_active_indices = None
        self._set_scan_tile_indicators([], None)

    def _set_scan_tile_indicators(self, routed, locked_tile_index):
        scanned_indices = {tile.index for tile, _segment in routed}
        for tile in self.scan_channel_tiles:
            scanning = tile.index in scanned_indices
            locked = scanning and (locked_tile_index is not None) and (tile.index == int(locked_tile_index))
            tile.set_scan_status(scanning, locked)

    def _process_scanner_frame(self, data):
        routed = self._route_scan_data_to_tiles(data)
        if not routed:
            if self.chk_spectrum.isChecked():
                self.spectrum_line.set_data(b"")
            self._set_scan_tile_indicators([], self.scan_lock_tile_index)
            return

        # Återgå till tidigare beteende: rita alltid kanalernas vattenfall i scannerläget.
        if self.chk_waterfall.isChecked():
            for tile, segment in routed:
                tile.consume_spectrum(segment)
        self._set_scan_tile_indicators(routed, self.scan_lock_tile_index)

        if self.scan_lock_tile_index is not None:
            locked_pair = None
            for tile, segment in routed:
                if tile.index == self.scan_lock_tile_index:
                    locked_pair = (tile, segment)
                    break
            if locked_pair is None:
                if self.chk_spectrum.isChecked():
                    self.spectrum_line.set_data(b"")
                self._unlock_scanner()
                return
            tile, segment = locked_pair
            has_signal = tile.detect_signal(segment)
            if has_signal:
                self.scan_unlock_miss_count = 0
                if self.chk_spectrum.isChecked():
                    # Visa "infångad ljudvåg" (den låsta kanalens spektruminnehåll).
                    self.spectrum_line.set_data(segment)
                return
            self.scan_unlock_miss_count += 1
            if self.chk_spectrum.isChecked():
                self.spectrum_line.set_data(b"")
            if self.scan_unlock_miss_count >= self.scan_unlock_miss_frames:
                self._unlock_scanner()
            return

        best_tile = None
        best_peak = -1
        for tile, segment in routed:
            if not tile.detect_signal(segment):
                continue
            peak = max(segment)
            if peak > best_peak:
                best_peak = peak
                best_tile = tile

        if best_tile is None:
            self.scan_lock_streak_tile_index = None
            self.scan_lock_hit_count = 0
            if self.chk_spectrum.isChecked():
                self.spectrum_line.set_data(b"")
            return

        if self.scan_lock_streak_tile_index == best_tile.index:
            self.scan_lock_hit_count += 1
        else:
            self.scan_lock_streak_tile_index = best_tile.index
            self.scan_lock_hit_count = 1

        if self.scan_lock_hit_count >= self.scan_lock_hit_frames:
            self._lock_scanner_to_tile(best_tile)
            if self.chk_spectrum.isChecked():
                for tile, segment in routed:
                    if tile.index == best_tile.index:
                        self.spectrum_line.set_data(segment)
                        break

    def _build_settings_payload(self):
        payload = {
            "mode": self._get_current_mode(),
            "fft_size": int(self.fft_combo.currentText()),
        }

        if payload["mode"] == "fixed":
            center_mhz = self._parse_float(self.center_input.text())
            bandwidth_mhz = max(0.001, self._parse_float(self.bandwidth_input.text()))
            payload["center"] = center_mhz * 1e6
            payload["bandwidth"] = bandwidth_mhz * 1e6
        elif payload["mode"] == "list_scan":
            channels = self._collect_scan_channels()
            if not any(ch["active"] for ch in channels):
                raise ValueError("Minst en aktiv scannerkanal krävs")
            dwell_ms = max(1.0, self._parse_float(self.scan_dwell_input.text()))
            payload["dwell_time"] = dwell_ms / 1000.0
            payload["scan_channels"] = [
                {
                    "center": ch["center_mhz"] * 1e6,
                    "bandwidth": ch["bandwidth_mhz"] * 1e6,
                    "active": ch["active"],
                    "auto_noise": ch["auto_noise"],
                    "noise_reduction_db": ch["noise_reduction_db"],
                }
                for ch in channels
            ]
        else:
            start_mhz = self._parse_float(self.start_input.text())
            stop_mhz = self._parse_float(self.stop_input.text())
            payload["start"] = start_mhz * 1e6
            payload["stop"] = stop_mhz * 1e6
            payload["step_size"] = max(0.001, self._parse_float(self.step_input.text())) * 1e6

        return payload

    def start_async(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_worker())

    async def network_worker(self):
        while True:
            if self.requested_server_shutdown:
                return
            try:
                async with websockets.connect(self.remote_url) as ws:
                    self.ws = ws
                    try:
                        # Skicka alltid startkonfig vid anslutning (inklusive default/CLI-värden).
                        await ws.send(json.dumps(self._build_settings_payload()))
                    except Exception as err:
                        print(f"Init send error: {err}")
                    while True:
                        if self.requested_server_shutdown:
                            return
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
                        elif isinstance(data, str):
                            try:
                                msg = json.loads(data)
                                if msg.get("event") == "server_shutdown":
                                    print("Servern har bekräftat shutdown.")
                            except Exception:
                                pass
            except:
                if self.requested_server_shutdown:
                    return
                await asyncio.sleep(2)

    def send_settings(self):
        try:
            self._reset_scan_lock()
            payload = self._build_settings_payload()
            self._apply_current_range_to_visuals()
            if self.ws:
                msg = json.dumps(payload)
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
                self._send_pause_state()
                if payload.get("mode") == "list_scan":
                    active_indices = {
                        idx for idx, ch in enumerate(payload.get("scan_channels", []))
                        if bool(ch.get("active", False))
                    }
                    self.runtime_scan_active_indices = active_indices
        except Exception as err: print(f"Input Error: {err}")

    def export_waterfall(self):
        default_name = "waterfall.png"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Exportera vattenfall",
            default_name,
            "PNG-bild (*.png);;JPEG-bild (*.jpg *.jpeg);;Alla filer (*)"
        )
        if not filename:
            return
        image = self.waterfall.snapshot_image()
        if not image.save(filename):
            print(f"Kunde inte spara bild: {filename}")

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
    parser.add_argument("--mode", choices=["sweep", "fixed", "list_scan"], default="sweep", help="Läge: svep, fast center eller scannerlista")
    parser.add_argument("--center", type=float, default=None, help="Centerfrekvens i MHz (fast-läge)")
    parser.add_argument("--bandwidth", type=float, default=None, help="Bandbredd i MHz (fast-läge)")
    parser.add_argument("--scan-centers", default=None, help="Kommaseparerad centerlista i MHz (scannerlista)")
    parser.add_argument("--scan-bandwidth", type=float, default=None, help="Bandbredd i MHz per center (scannerlista)")
    parser.add_argument("--scan-dwell-ms", type=float, default=None, help="Aktiv tid i millisekunder per frekvens (scannerlista)")
    parser.add_argument("--fft", type=int, choices=[256, 512, 1024, 2048, 4096], default=1024, help="FFT-storlek")
    args = parser.parse_args()
    remote_url = normalize_remote_target(args.remote)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow(
        remote_url,
        args.start,
        args.stop,
        args.step,
        args.fft,
        initial_mode=args.mode,
        center_mhz=args.center,
        bandwidth_mhz=args.bandwidth,
        scan_centers_csv=args.scan_centers,
        scan_bandwidth_mhz=args.scan_bandwidth,
        scan_dwell_ms=args.scan_dwell_ms,
    )
    win.show()
    sys.exit(app.exec())
