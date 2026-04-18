import sys
import types
import logging
import ctypes

# --- FIXAR FÖR PYTHON 3.13+ OCH LIBRTLSDR ---
if 'pkg_resources' not in sys.modules:
    dummy_pkg = types.ModuleType('pkg_resources')
    dummy_pkg.get_distribution = lambda name: types.SimpleNamespace(version="0.3.0")
    sys.modules['pkg_resources'] = dummy_pkg

try:
    import rtlsdr.librtlsdr as librtlsdr_mod

    def dummy_set_dithering(device, on):
        return 0

    setattr(librtlsdr_mod.librtlsdr, 'rtlsdr_set_dithering', dummy_set_dithering)
except:
    pass

from rtlsdr import RtlSdr
import asyncio
import websockets
import numpy as np
import json

logger = logging.getLogger(__name__)

sdr = RtlSdr()
sdr.gain = 'auto'
shutdown_event = None

# Globalt tillstånd för scanningen
state = {
    "mode": "sweep",  # "sweep", "fixed" eller "list_scan"
    "start": 88e6,
    "stop": 108e6,
    "center": 98e6,
    "bandwidth": 20e6,
    "scan_centers": [98e6],
    "scan_channels": [
        {"center": 98e6, "bandwidth": 2.4e6, "active": True, "auto_noise": False, "noise_reduction_db": -35.0}
    ],
    "dwell_time": 0.08,
    "fft_size": 1024,
    "step_size": 1.5e6,
    "sample_rate": 2.4e6,
    "paused": True,
}


def _sanitize_positive(value, fallback):
    try:
        value = float(value)
    except Exception:
        return float(fallback)
    if value <= 0:
        return float(fallback)
    return value


def _sanitize_frequency_list(values, fallback):
    if not isinstance(values, list):
        return list(fallback)
    out = []
    for item in values:
        try:
            out.append(float(item))
        except Exception:
            continue
    if not out:
        return list(fallback)
    return out


def _sanitize_scan_channels(values, fallback):
    if not isinstance(values, list):
        return [dict(ch) for ch in fallback]
    out = []
    for item in values:
        if not isinstance(item, dict):
            continue
        center = item.get("center")
        bandwidth = item.get("bandwidth")
        if center is None or bandwidth is None:
            continue
        try:
            center = float(center)
            bandwidth = float(bandwidth)
        except Exception:
            continue
        if bandwidth <= 0:
            continue
        out.append(
            {
                "center": center,
                "bandwidth": bandwidth,
                "active": bool(item.get("active", True)),
                "auto_noise": bool(item.get("auto_noise", False)),
                "noise_reduction_db": float(item.get("noise_reduction_db", -35.0)),
            }
        )
    if not out:
        return [dict(ch) for ch in fallback]
    return out


def _update_state_from_payload(data):
    mode_changed = False

    if "mode" in data:
        mode = str(data["mode"]).strip().lower()
        if mode in ("sweep", "fixed", "list_scan") and mode != state.get("mode"):
            state["mode"] = mode
            mode_changed = True

    if "fft_size" in data:
        state["fft_size"] = max(64, int(data["fft_size"]))

    if "step_size" in data:
        state["step_size"] = _sanitize_positive(data["step_size"], state["step_size"])

    if "start" in data:
        state["start"] = float(data["start"])

    if "stop" in data:
        state["stop"] = float(data["stop"])

    if "center" in data:
        state["center"] = float(data["center"])

    if "bandwidth" in data:
        state["bandwidth"] = _sanitize_positive(data["bandwidth"], state["bandwidth"])

    if "scan_centers" in data:
        state["scan_centers"] = _sanitize_frequency_list(data["scan_centers"], state["scan_centers"])
        # Bakåtkompatibilitet: bygg kanallista från centers om ny struktur saknas.
        if "scan_channels" not in data:
            state["scan_channels"] = [
                {
                    "center": center,
                    "bandwidth": float(state.get("bandwidth", 2.4e6)),
                    "active": True,
                    "auto_noise": False,
                    "noise_reduction_db": -35.0,
                }
                for center in state["scan_centers"]
            ]

    if "scan_channels" in data:
        state["scan_channels"] = _sanitize_scan_channels(data["scan_channels"], state["scan_channels"])

    if "dwell_time" in data:
        state["dwell_time"] = _sanitize_positive(data["dwell_time"], state["dwell_time"])

    if "sample_rate" in data:
        state["sample_rate"] = _sanitize_positive(data["sample_rate"], state["sample_rate"])

    if "paused" in data:
        state["paused"] = bool(data["paused"])
    elif mode_changed:
        # Modebyte ska inte börja köra automatiskt.
        state["paused"] = True


def get_clean_spectrum(samples, fft_size, visible_bw, sample_rate):
    window = np.hamming(len(samples))
    fft_data = np.fft.fftshift(np.fft.fft(samples * window))
    power_db = 10 * np.log10(np.abs(fft_data) ** 2 + 1e-9)

    # Behåll den del av FFT:n som motsvarar synlig bandbredd.
    keep_ratio = max(1e-3, min(1.0, float(visible_bw) / float(sample_rate)))
    margin = int((1 - keep_ratio) / 2 * fft_size)
    return power_db[margin: fft_size - margin]


async def sdr_handler(websocket):
    global shutdown_event
    logger.info(f"Klient ansluten {str(websocket.remote_address)}")
    try:
        while not shutdown_event.is_set():
            # 1. Kolla efter nya parametrar
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                data = json.loads(msg)
                if isinstance(data, dict):
                    if bool(data.get("shutdown")):
                        logger.info("Shutdown-begäran mottagen från klient.")
                        shutdown_event.set()
                        try:
                            await websocket.send(json.dumps({"event": "server_shutdown"}))
                        except:
                            pass
                        break
                    _update_state_from_payload(data)

                sdr.sample_rate = state["sample_rate"]
                logger.info(f"{str(websocket.remote_address)} Uppdaterad konfig: {state}")
            except:
                pass

            if shutdown_event.is_set():
                break

            if state["paused"]:
                await asyncio.sleep(0.05)
                continue

            full_spectrum = []
            mode = state.get("mode", "sweep")

            if mode == "fixed":
                center = float(state["center"])
                bandwidth = min(float(state["bandwidth"]), float(state["sample_rate"]))
                sdr.center_freq = center
                await asyncio.sleep(0.01)
                samples = sdr.read_samples(state["fft_size"])
                full_spectrum.append(
                    get_clean_spectrum(samples, state["fft_size"], bandwidth, state["sample_rate"])
                )
            elif mode == "list_scan":
                channels = _sanitize_scan_channels(
                    state.get("scan_channels", []),
                    [{"center": float(state["center"]), "bandwidth": float(state["bandwidth"]), "active": True, "auto_noise": False, "noise_reduction_db": -35.0}],
                )
                active_channels = [ch for ch in channels if bool(ch.get("active", True))]
                if not active_channels:
                    await asyncio.sleep(0.05)
                    continue
                dwell_time = max(0.001, float(state.get("dwell_time", 0.08)))
                for channel in active_channels:
                    if shutdown_event.is_set():
                        break
                    center = float(channel["center"])
                    bandwidth = min(float(channel["bandwidth"]), float(state["sample_rate"]))
                    sdr.center_freq = center
                    await asyncio.sleep(dwell_time)
                    samples = sdr.read_samples(state["fft_size"])
                    full_spectrum.append(
                        get_clean_spectrum(samples, state["fft_size"], bandwidth, state["sample_rate"])
                    )
            else:
                curr = float(state["start"])
                stop = float(state["stop"])
                step = max(1.0, float(state["step_size"]))

                if curr > stop:
                    curr, stop = stop, curr

                # 2. Scanning-loop
                while curr <= stop:
                    if shutdown_event.is_set():
                        break
                    sdr.center_freq = curr
                    await asyncio.sleep(0.01)  # Snabbare switch för live-känsla

                    samples = sdr.read_samples(state["fft_size"])
                    full_spectrum.append(
                        get_clean_spectrum(samples, state["fft_size"], step, state["sample_rate"])
                    )
                    curr += step

            if full_spectrum:
                combined = np.concatenate(full_spectrum)
                normalized = np.clip((combined + 60) * 3, 0, 255).astype(np.uint8)
                await websocket.send(normalized.tobytes())

    except Exception as e:
        logger.error(f"Fel: {e}")


async def main():
    global shutdown_event
    shutdown_event = asyncio.Event()
    server = await websockets.serve(sdr_handler, "0.0.0.0", 8765)
    try:
        logger.info("Server redo...")
        await shutdown_event.wait()
        logger.info("Stänger ner server...")
    finally:
        server.close()
        await server.wait_closed()
        try:
            sdr.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
