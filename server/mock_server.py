import asyncio
import json
import threading
import time
from dataclasses import dataclass, asdict

import numpy as np
import websockets
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


@dataclass
class MockConfig:
    start: float = 88e6
    stop: float = 108e6
    fft_size: int = 1024
    step_size: float = 1.5e6
    sample_rate: float = 2.4e6
    noise_floor_db: float = -92.0
    noise_jitter_db: float = 4.0
    peak_count: int = 8
    frame_interval_s: float = 0.06


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg = MockConfig()

    def snapshot(self) -> MockConfig:
        with self._lock:
            return MockConfig(**asdict(self._cfg))

    def update_from_client(self, data: dict) -> None:
        with self._lock:
            if "start" in data:
                self._cfg.start = float(data["start"])
            if "stop" in data:
                self._cfg.stop = float(data["stop"])
            if "fft_size" in data:
                self._cfg.fft_size = max(64, int(data["fft_size"]))
            if "step_size" in data:
                self._cfg.step_size = max(1.0, float(data["step_size"]))
            if "sample_rate" in data:
                self._cfg.sample_rate = max(1.0, float(data["sample_rate"]))

    def set_noise_floor(self, noise_floor_db: float) -> None:
        with self._lock:
            self._cfg.noise_floor_db = float(noise_floor_db)

    def set_noise_jitter(self, noise_jitter_db: float) -> None:
        with self._lock:
            self._cfg.noise_jitter_db = float(noise_jitter_db)

    def set_peak_count(self, peak_count: int) -> None:
        with self._lock:
            self._cfg.peak_count = int(peak_count)


class MockSpectrumEngine:
    def __init__(self) -> None:
        self.rng = np.random.default_rng()

    @staticmethod
    def _bins_per_step(cfg: MockConfig) -> int:
        keep_ratio = cfg.step_size / cfg.sample_rate
        keep_ratio = float(np.clip(keep_ratio, 0.05, 1.0))
        margin = int((1.0 - keep_ratio) / 2.0 * cfg.fft_size)
        bins = cfg.fft_size - (2 * margin)
        return max(16, bins)

    @staticmethod
    def _num_steps(cfg: MockConfig) -> int:
        if cfg.step_size <= 0:
            return 1
        span = max(0.0, cfg.stop - cfg.start)
        return max(1, int(np.floor(span / cfg.step_size)) + 1)

    def generate(self, cfg: MockConfig) -> np.ndarray:
        bins_per_step = self._bins_per_step(cfg)
        total_bins = bins_per_step * self._num_steps(cfg)

        # Slumpmässigt bakgrundsbrus med styrbar nivå och spridning.
        spectrum = self.rng.normal(
            loc=cfg.noise_floor_db,
            scale=max(0.1, cfg.noise_jitter_db),
            size=total_bins,
        )

        # Lägg in syntetiska toppar så vattenfallet ser levande ut.
        if cfg.peak_count > 0 and total_bins > 0:
            x = np.arange(total_bins)
            for _ in range(cfg.peak_count):
                center = self.rng.integers(0, total_bins)
                width = self.rng.uniform(5.0, 70.0)
                amplitude = self.rng.uniform(8.0, 32.0)
                spectrum += amplitude * np.exp(-0.5 * ((x - center) / width) ** 2)

        return np.clip(spectrum, -120.0, 20.0)


class MockServerWindow(QMainWindow):
    def __init__(self, state: SharedState) -> None:
        super().__init__()
        self.state = state
        self.setWindowTitle("SDR Mock Server")
        self.resize(520, 260)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        info = QLabel(
            "WebSocket: ws://0.0.0.0:8765\n"
            "Mocken tar emot samma svep-parametrar som riktiga servern."
        )
        info.setStyleSheet("font-size: 13px;")
        layout.addWidget(info)

        group = QGroupBox("Signalmodell")
        grid = QGridLayout(group)

        self.noise_label = QLabel()
        self.noise_slider = QSlider(Qt.Horizontal)
        self.noise_slider.setRange(-110, -40)
        self.noise_slider.setValue(-92)
        self.noise_slider.valueChanged.connect(self._on_noise_changed)

        self.jitter_label = QLabel()
        self.jitter_slider = QSlider(Qt.Horizontal)
        self.jitter_slider.setRange(1, 20)
        self.jitter_slider.setValue(4)
        self.jitter_slider.valueChanged.connect(self._on_jitter_changed)

        self.peaks_label = QLabel()
        self.peaks_slider = QSlider(Qt.Horizontal)
        self.peaks_slider.setRange(0, 24)
        self.peaks_slider.setValue(8)
        self.peaks_slider.valueChanged.connect(self._on_peaks_changed)

        grid.addWidget(QLabel("Bakgrundsbrus (dB):"), 0, 0)
        grid.addWidget(self.noise_slider, 0, 1)
        grid.addWidget(self.noise_label, 0, 2)

        grid.addWidget(QLabel("Brusvariation (dB):"), 1, 0)
        grid.addWidget(self.jitter_slider, 1, 1)
        grid.addWidget(self.jitter_label, 1, 2)

        grid.addWidget(QLabel("Antal toppar:"), 2, 0)
        grid.addWidget(self.peaks_slider, 2, 1)
        grid.addWidget(self.peaks_label, 2, 2)

        layout.addWidget(group)

        self.stop_button = QPushButton("Stang")
        self.stop_button.clicked.connect(self.close)
        layout.addWidget(self.stop_button)

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        self.noise_label.setText(str(self.noise_slider.value()))
        self.jitter_label.setText(str(self.jitter_slider.value()))
        self.peaks_label.setText(str(self.peaks_slider.value()))

    def _on_noise_changed(self, value: int) -> None:
        self.state.set_noise_floor(float(value))
        self._refresh_labels()

    def _on_jitter_changed(self, value: int) -> None:
        self.state.set_noise_jitter(float(value))
        self._refresh_labels()

    def _on_peaks_changed(self, value: int) -> None:
        self.state.set_peak_count(value)
        self._refresh_labels()


async def client_handler(websocket, state: SharedState, engine: MockSpectrumEngine) -> None:
    print("Klient ansluten till mock-server")
    try:
        while True:
            # Plocka upp ny konfig om klienten skickar något.
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.001)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    state.update_from_client(data)
            except asyncio.TimeoutError:
                pass
            except websockets.exceptions.ConnectionClosed:
                raise
            except Exception:
                pass

            cfg = state.snapshot()
            spectrum_db = engine.generate(cfg)
            normalized = np.clip((spectrum_db + 60.0) * 3.0, 0, 255).astype(np.uint8)
            await websocket.send(normalized.tobytes())
            await asyncio.sleep(cfg.frame_interval_s)
    except websockets.exceptions.ConnectionClosed:
        print("Klient kopplade ner")


async def run_server(state: SharedState, stop_event: threading.Event) -> None:
    engine = MockSpectrumEngine()

    async def _handler(ws):
        await client_handler(ws, state, engine)

    async with websockets.serve(_handler, "0.0.0.0", 8765):
        print("Mock-server lyssnar pa ws://0.0.0.0:8765")
        while not stop_event.is_set():
            await asyncio.sleep(0.1)


def server_thread_main(state: SharedState, stop_event: threading.Event) -> None:
    asyncio.run(run_server(state, stop_event))


def main() -> None:
    state = SharedState()
    stop_event = threading.Event()

    thread = threading.Thread(target=server_thread_main, args=(state, stop_event), daemon=True)
    thread.start()

    app = QApplication([])
    win = MockServerWindow(state)
    win.show()
    app.exec()

    stop_event.set()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
