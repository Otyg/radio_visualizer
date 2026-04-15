import sys
import json
import asyncio
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel, QSlider)
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import Qt, Signal, Slot

import websockets

class WaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(500)
        # Vi börjar med en standardstorlek, den anpassas när data kommer
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0

    def add_line(self, data_bytes, threshold):
        width = len(data_bytes)
        if width == 0: return

        # Om servern skickar en ny bredd (pga ändrat frekvensspann), skapa ny bild
        if self.image.width() != width:
            self.image = QImage(width, 1000, QImage.Format_RGB32)
            self.image.fill(Qt.black)
            self.current_row = 0

        for x in range(width):
            val = data_bytes[x]
            
            if val < threshold:
                # Under tröskelvärdet = Svart
                self.image.setPixelColor(x, self.current_row, QColor(0, 0, 0))
            else:
                # Skala om värdet (0-255) baserat på tröskeln för bättre kontrast
                # Allt från tröskel upp till 255 sträcks ut över färgskalan
                norm_val = int(((val - threshold) / (255 - threshold)) * 255)
                norm_val = max(0, min(255, norm_val)) # Säkerhetsmarginal

                # En klassisk "Inferno/SDR" heatmap:
                # Låg (Blå) -> Mellan (Lila/Röd) -> Hög (Gul/Vit)
                r = int(norm_val * 1.0)
                g = int(norm_val * 0.7) if norm_val > 100 else 0
                b = int(255 - norm_val * 0.5) if norm_val < 150 else int(norm_val * 0.2)
                
                self.image.setPixelColor(x, self.current_row, QColor(r, g, b))

        # Flytta skrivhuvudet och rulla runt om vi når botten
        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Vi ritar bilden så att den nyaste raden alltid hamnar i rätt ordning.
        # För enkelhetens skull ritar vi hela bilden, men du kan optimera 
        # genom att rita den i två delar för att få en rullande effekt.
        target_rect = self.rect()
        painter.drawImage(target_rect, self.image)

class MainWindow(QMainWindow):
    # Signal för att skicka data från nätverkstråden till UI-tråden
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gemini SDR Wideband Scanner")
        self.resize(1200, 800)
        
        # --- UI LAYOUT ---
        main_layout = QVBoxLayout()
        ctrl_layout = QHBoxLayout()
        
        # Frekvensinställningar
        self.start_freq = QLineEdit("88000000")
        self.stop_freq = QLineEdit("108000000")
        self.server_ip = QLineEdit("localhost")
        self.btn_connect = QPushButton("Uppdatera Scan")
        self.btn_connect.setStyleSheet("background-color: #2b5; color: white; font-weight: bold;")
        
        # Tröskelvärde / Brusnivå
        self.threshold_label = QLabel("Bruströskel: 40")
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 200)
        self.threshold_slider.setValue(40)
        self.threshold_slider.setFixedWidth(200)
        self.threshold_slider.valueChanged.connect(self.update_threshold_label)

        # Packa kontrollpanelen
        ctrl_layout.addWidget(QLabel("Server:"))
        ctrl_layout.addWidget(self.server_ip)
        ctrl_layout.addWidget(QLabel("Start (Hz):"))
        ctrl_layout.addWidget(self.start_freq)
        ctrl_layout.addWidget(QLabel("Stopp (Hz):"))
        ctrl_layout.addWidget(self.stop_freq)
        ctrl_layout.addWidget(self.btn_connect)
        ctrl_layout.addSpacing(20)
        ctrl_layout.addWidget(self.threshold_label)
        ctrl_layout.addWidget(self.threshold_slider)
        
        # Vattenfall
        self.waterfall = WaterfallWidget()
        
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(self.waterfall)
        
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        
        # --- EVENT KOPPLINGAR ---
        self.btn_connect.clicked.connect(self.send_settings)
        self.data_received.connect(self.handle_new_data)
        
        # --- NÄTVERKSTRÅD ---
        self.loop = asyncio.new_event_loop()
        self.ws = None
        threading.Thread(target=self.start_async_loop, daemon=True).start()

    def update_threshold_label(self, val):
        self.threshold_label.setText(f"Bruströskel: {val}")

    @Slot(bytes)
    def handle_new_data(self, data):
        # Skicka vidare data plus nuvarande tröskelvärde till widgeten
        self.waterfall.add_line(data, self.threshold_slider.value())

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_handler())

    async def network_handler(self):
        while True:
            try:
                uri = f"ws://{self.server_ip.text()}:8765"
                print(f"Försöker ansluta till {uri}...")
                async with websockets.connect(uri) as ws:
                    self.ws = ws
                    print("Ansluten!")
                    while True:
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
            except Exception as e:
                print(f"Anslutningsfel: {e}")
                await asyncio.sleep(3) # Vänta innan omstart

    def send_settings(self):
        if self.ws:
            try:
                msg = json.dumps({
                    "start": float(self.start_freq.text()),
                    "stop": float(self.stop_freq.text())
                })
                # Kör coroutinen i den rullande loopen
                asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
                print("Inställningar skickade till server.")
            except ValueError:
                print("Felaktigt frekvensformat.")
        else:
            print("Inte ansluten till server än.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Sätt ett mörkt tema för hela appen
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())