import sys
import json
import asyncio
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLineEdit, QPushButton, QLabel)
from PySide6.QtGui import QImage, QPainter, QColor, QPixmap
from PySide6.QtCore import Qt, Signal, Slot, QPoint
import websockets

class WaterfallWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(400)
        self.image = QImage(1024, 1000, QImage.Format_RGB32)
        self.image.fill(Qt.black)
        self.current_row = 0

    def add_line(self, data_bytes):
        width = len(data_bytes)
        if width == 0: return

        # Skapa ny QImage om bredden ändrats (pga nytt frekvensspann)
        if self.image.width() != width:
            self.image = QImage(width, 1000, QImage.Format_RGB32)
            self.image.fill(Qt.black)
            self.current_row = 0

        # Rita den nya raden i bilden
        for x in range(width):
            val = data_bytes[x]
            # Enkel heatmap: Blå -> Grön -> Röd
            r = val if val > 150 else 0
            g = val if 50 < val < 200 else (255 if val >= 200 else 0)
            b = 255 - val if val < 150 else 0
            self.image.setPixelColor(x, self.current_row, QColor(r, g, b))

        self.current_row = (self.current_row + 1) % self.image.height()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        # Rita bilden i två delar för att skapa en rullande effekt
        h = self.image.height()
        split = self.current_row
        
        # Rita den nyaste delen överst och den äldre under
        # Detta skapar en "infinite scroll"-effekt
        rect_top = self.rect()
        painter.drawImage(self.rect(), self.image, self.image.rect())

class MainWindow(QMainWindow):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Qt SDR Wideband Scanner")
        
        # UI Setup
        main_layout = QVBoxLayout()
        ctrl_layout = QHBoxLayout()
        
        self.start_freq = QLineEdit("88000000")
        self.stop_freq = QLineEdit("108000000")
        self.server_ip = QLineEdit("localhost")
        self.btn_connect = QPushButton("Anslut & Uppdatera")
        
        ctrl_layout.addWidget(QLabel("Server IP:"))
        ctrl_layout.addWidget(self.server_ip)
        ctrl_layout.addWidget(QLabel("Start (Hz):"))
        ctrl_layout.addWidget(self.start_freq)
        ctrl_layout.addWidget(QLabel("Stopp (Hz):"))
        ctrl_layout.addWidget(self.stop_freq)
        ctrl_layout.addWidget(self.btn_connect)
        
        self.waterfall = WaterfallWidget()
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(self.waterfall)
        
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        
        self.btn_connect.clicked.connect(self.send_settings)
        self.data_received.connect(self.waterfall.add_line)
        
        # Starta nätverkstråd
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.start_async_loop, daemon=True).start()

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.network_handler())

    async def network_handler(self):
        while True:
            try:
                uri = f"ws://{self.server_ip.text()}:8765"
                async with websockets.connect(uri) as ws:
                    self.ws = ws
                    while True:
                        data = await ws.recv()
                        if isinstance(data, bytes):
                            self.data_received.emit(data)
            except Exception as e:
                print(f"Anslutningsfel: {e}")
                await asyncio.sleep(2)

    def send_settings(self):
        if hasattr(self, 'ws'):
            msg = json.dumps({
                "start": float(self.start_freq.text()),
                "stop": float(self.stop_freq.text())
            })
            asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())