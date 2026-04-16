import asyncio
import copy
import json
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import websockets
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)


@dataclass
class TransmitterConfig:
    tx_id: int
    enabled: bool = True
    base_freq_hz: float = 98e6
    modulation: str = "FM"
    power_db: float = -55.0


@dataclass
class MockConfig:
    start: float = 88e6
    stop: float = 108e6
    fft_size: int = 1024
    step_size: float = 1.5e6
    sample_rate: float = 2.4e6
    noise_floor_db: float = -92.0
    noise_jitter_db: float = 4.0
    frame_interval_s: float = 0.06
    transmitters: list[TransmitterConfig] = field(
        default_factory=lambda: [
            TransmitterConfig(tx_id=1, enabled=True, base_freq_hz=96.8e6, modulation="FM", power_db=-55.0)
        ]
    )


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg = MockConfig()

    def snapshot(self) -> MockConfig:
        with self._lock:
            return copy.deepcopy(self._cfg)

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

    def set_transmitters(self, transmitters: list[TransmitterConfig]) -> None:
        with self._lock:
            self._cfg.transmitters = copy.deepcopy(transmitters)


class MockSpectrumEngine:
    def __init__(self) -> None:
        self.rng = np.random.default_rng()
        self._phase_by_tx: dict[int, float] = {}
        self._phase_step_by_tx: dict[int, float] = {}

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

    def _next_mod_signal(self, tx_id: int) -> float:
        phase = self._phase_by_tx.get(tx_id)
        if phase is None:
            phase = float(self.rng.uniform(0.0, 2.0 * np.pi))
            self._phase_step_by_tx[tx_id] = float(self.rng.normal(0.24, 0.03))

        # Modulationssignal: sinus med normalfordelad slumpkomponent.
        gauss_sample = float(self.rng.normal(0.0, 1.0))
        mod_signal = float(np.sin(phase) + 0.30 * gauss_sample)

        phase_step = self._phase_step_by_tx.get(tx_id, 0.24)
        phase_step += float(self.rng.normal(0.0, 0.004))
        phase_step = float(np.clip(phase_step, 0.08, 0.5))
        self._phase_step_by_tx[tx_id] = phase_step
        self._phase_by_tx[tx_id] = phase + phase_step
        return float(np.clip(mod_signal, -1.5, 1.5))

    @staticmethod
    def _add_gaussian(spectrum: np.ndarray, center_bin: float, sigma_bins: float, gain_db: float) -> None:
        if gain_db <= 0.0 or sigma_bins <= 0.0:
            return
        x = np.arange(spectrum.size)
        shape = np.exp(-0.5 * ((x - center_bin) / sigma_bins) ** 2)
        spectrum += gain_db * shape

    def _apply_transmitter(self, spectrum: np.ndarray, cfg: MockConfig, tx: TransmitterConfig) -> None:
        if not tx.enabled:
            return
        span_hz = max(1.0, cfg.stop - cfg.start)
        if tx.base_freq_hz < cfg.start - span_hz * 0.05 or tx.base_freq_hz > cfg.stop + span_hz * 0.05:
            return

        hz_per_bin = span_hz / max(1, spectrum.size)
        # Sändarstyrkan styr amplituden pa carriern direkt relativt brusgolvet.
        carrier_gain = max(0.0, tx.power_db - cfg.noise_floor_db)
        if carrier_gain <= 0.0:
            return
        mod_signal = self._next_mod_signal(tx.tx_id)

        base_bin = (tx.base_freq_hz - cfg.start) / hz_per_bin
        sigma_carrier = 0.8
        self._add_gaussian(spectrum, base_bin, sigma_carrier, carrier_gain)

        if tx.modulation.upper() == "AM":
            modulation_depth = float(np.clip(0.2 + 0.45 * abs(mod_signal), 0.0, 1.0))
            side_offset = 2.5 + 9.0 * abs(mod_signal)
            side_gain_1 = carrier_gain * modulation_depth * 0.48
            side_gain_2 = carrier_gain * modulation_depth * 0.22
            self._add_gaussian(spectrum, base_bin - side_offset, 1.1, side_gain_1)
            self._add_gaussian(spectrum, base_bin + side_offset, 1.1, side_gain_1)
            self._add_gaussian(spectrum, base_bin - 2.0 * side_offset, 1.4, side_gain_2)
            self._add_gaussian(spectrum, base_bin + 2.0 * side_offset, 1.4, side_gain_2)
        else:
            freq_dev_hz = 0.05 * cfg.step_size * mod_signal
            inst_bin = (tx.base_freq_hz + freq_dev_hz - cfg.start) / hz_per_bin
            self._add_gaussian(spectrum, inst_bin, 0.95, carrier_gain * 0.55)

            modulation_index = 0.45 + 1.1 * abs(mod_signal)
            side_spacing = 2.0 + 7.0 * abs(mod_signal)
            side_gain_1 = carrier_gain * min(0.72, 0.33 * modulation_index)
            side_gain_2 = carrier_gain * min(0.44, 0.18 * modulation_index)
            self._add_gaussian(spectrum, base_bin - side_spacing, 1.1, side_gain_1)
            self._add_gaussian(spectrum, base_bin + side_spacing, 1.1, side_gain_1)
            self._add_gaussian(spectrum, base_bin - 2.0 * side_spacing, 1.5, side_gain_2)
            self._add_gaussian(spectrum, base_bin + 2.0 * side_spacing, 1.5, side_gain_2)

    def generate(self, cfg: MockConfig) -> np.ndarray:
        bins_per_step = self._bins_per_step(cfg)
        total_bins = bins_per_step * self._num_steps(cfg)

        # Bakgrundsbruset ska alltid vara slumpmassigt.
        # "Brusvariation" styr extra spridning ovanpa en liten basjitter.
        noise_sigma = 0.35 + max(0.0, cfg.noise_jitter_db)
        spectrum = self.rng.normal(
            loc=cfg.noise_floor_db,
            scale=noise_sigma,
            size=total_bins,
        )

        for tx in cfg.transmitters:
            self._apply_transmitter(spectrum, cfg, tx)

        return np.clip(spectrum, -120.0, 20.0)


class TransmitterRow(QWidget):
    def __init__(self, tx_id: int, on_change, on_remove) -> None:
        super().__init__()
        self.tx_id = tx_id
        self._on_change = on_change
        self._on_remove = on_remove

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.enabled = QCheckBox(f"TX {tx_id}")
        self.enabled.setChecked(True)

        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(3)
        self.freq.setRange(0.1, 6000.0)
        self.freq.setSingleStep(0.1)
        self.freq.setSuffix(" MHz")
        self.freq.setValue(96.8)

        self.modulation = QComboBox()
        self.modulation.addItems(["FM", "AM"])

        self.power = QSlider(Qt.Horizontal)
        self.power.setRange(-110, 20)
        self.power.setValue(-55)
        self.power_label = QLabel("-55 dB")
        self.power_label.setFixedWidth(54)

        self.btn_remove = QPushButton("Ta bort")

        layout.addWidget(self.enabled)
        layout.addWidget(self.freq)
        layout.addWidget(self.modulation)
        layout.addWidget(QLabel("Styrka:"))
        layout.addWidget(self.power, 1)
        layout.addWidget(self.power_label)
        layout.addWidget(self.btn_remove)

        self.enabled.stateChanged.connect(self._signal_change)
        self.freq.valueChanged.connect(self._signal_change)
        self.modulation.currentTextChanged.connect(self._signal_change)
        self.power.valueChanged.connect(self._on_power_changed)
        self.btn_remove.clicked.connect(self._remove_self)

    def set_values(self, tx: TransmitterConfig) -> None:
        self.enabled.setChecked(tx.enabled)
        self.freq.setValue(tx.base_freq_hz / 1e6)
        self.modulation.setCurrentText(tx.modulation.upper())
        self.power.setValue(int(round(tx.power_db)))
        self.power_label.setText(f"{int(round(tx.power_db))} dB")

    def get_config(self) -> TransmitterConfig:
        return TransmitterConfig(
            tx_id=self.tx_id,
            enabled=self.enabled.isChecked(),
            base_freq_hz=float(self.freq.value()) * 1e6,
            modulation=self.modulation.currentText(),
            power_db=float(self.power.value()),
        )

    def _on_power_changed(self, value: int) -> None:
        self.power_label.setText(f"{value} dB")
        self._signal_change()

    def _signal_change(self) -> None:
        self._on_change()

    def _remove_self(self) -> None:
        self._on_remove(self)


class MockServerWindow(QMainWindow):
    def __init__(self, state: SharedState) -> None:
        super().__init__()
        self.state = state
        self._tx_counter = 0
        self.tx_rows: list[TransmitterRow] = []

        self.setWindowTitle("SDR Mock Server")
        self.resize(860, 520)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        info = QLabel(
            "WebSocket: ws://0.0.0.0:8765\\n"
            "Mocken tar emot samma svep-parametrar som riktiga servern."
        )
        info.setStyleSheet("font-size: 13px;")
        layout.addWidget(info)

        model_group = QGroupBox("Bakgrund")
        model_grid = QGridLayout(model_group)

        self.noise_label = QLabel()
        self.noise_slider = QSlider(Qt.Horizontal)
        self.noise_slider.setRange(-110, 0)
        self.noise_slider.setValue(-92)
        self.noise_slider.valueChanged.connect(self._on_noise_changed)

        self.jitter_label = QLabel()
        self.jitter_slider = QSlider(Qt.Horizontal)
        self.jitter_slider.setRange(0, 20)
        self.jitter_slider.setValue(4)
        self.jitter_slider.valueChanged.connect(self._on_jitter_changed)

        model_grid.addWidget(QLabel("Bakgrundsbrus (dB):"), 0, 0)
        model_grid.addWidget(self.noise_slider, 0, 1)
        model_grid.addWidget(self.noise_label, 0, 2)

        model_grid.addWidget(QLabel("Brusvariation (dB):"), 1, 0)
        model_grid.addWidget(self.jitter_slider, 1, 1)
        model_grid.addWidget(self.jitter_label, 1, 2)

        layout.addWidget(model_group)

        tx_group = QGroupBox("Sandare")
        tx_layout = QVBoxLayout(tx_group)

        tx_header = QHBoxLayout()
        self.add_tx_button = QPushButton("Lagg till sandare")
        self.add_tx_button.clicked.connect(self._add_transmitter)
        tx_header.addWidget(self.add_tx_button)
        tx_header.addStretch()
        tx_layout.addLayout(tx_header)

        self.tx_container = QWidget()
        self.tx_rows_layout = QVBoxLayout(self.tx_container)
        self.tx_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.tx_rows_layout.setSpacing(8)
        self.tx_rows_layout.addStretch()

        tx_scroll = QScrollArea()
        tx_scroll.setWidgetResizable(True)
        tx_scroll.setWidget(self.tx_container)
        tx_layout.addWidget(tx_scroll)

        layout.addWidget(tx_group, 1)

        self.stop_button = QPushButton("Stang")
        self.stop_button.clicked.connect(self.close)
        layout.addWidget(self.stop_button)

        self._refresh_labels()
        initial_cfg = self.state.snapshot()
        for tx in initial_cfg.transmitters:
            self._add_transmitter(tx)
        self._sync_transmitters_to_state()

    def _refresh_labels(self) -> None:
        self.noise_label.setText(str(self.noise_slider.value()))
        self.jitter_label.setText(str(self.jitter_slider.value()))

    def _on_noise_changed(self, value: int) -> None:
        self.state.set_noise_floor(float(value))
        self._refresh_labels()

    def _on_jitter_changed(self, value: int) -> None:
        self.state.set_noise_jitter(float(value))
        self._refresh_labels()

    def _add_transmitter(self, tx: TransmitterConfig | None = None) -> None:
        self._tx_counter += 1
        row = TransmitterRow(self._tx_counter, self._sync_transmitters_to_state, self._remove_transmitter)

        if tx is not None:
            row.set_values(tx)
        else:
            row.freq.setValue(95.0 + 0.7 * len(self.tx_rows))

        self.tx_rows.append(row)
        self.tx_rows_layout.insertWidget(self.tx_rows_layout.count() - 1, row)
        self._sync_transmitters_to_state()

    def _remove_transmitter(self, row: TransmitterRow) -> None:
        if row not in self.tx_rows:
            return
        self.tx_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._sync_transmitters_to_state()

    def _sync_transmitters_to_state(self) -> None:
        tx_cfg = [row.get_config() for row in self.tx_rows]
        self.state.set_transmitters(tx_cfg)


async def client_handler(websocket, state: SharedState, engine: MockSpectrumEngine) -> None:
    print("Klient ansluten till mock-server")
    try:
        while True:
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
