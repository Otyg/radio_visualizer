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
    def dummy_set_dithering(device, on): return 0
    setattr(librtlsdr_mod.librtlsdr, 'rtlsdr_set_dithering', dummy_set_dithering)
except: pass

from rtlsdr import RtlSdr
import asyncio
import websockets
import numpy as np
import json
logger = logging.getLogger(__name__)

sdr = RtlSdr()
sdr.gain = 'auto'

# Globalt tillstånd för scanningen
state = {
    "start": 88e6,
    "stop": 108e6,
    "fft_size": 1024,
    "step_size": 1.5e6,
    "sample_rate": 2.4e6,
    "paused": False,
}

def get_clean_spectrum(samples, fft_size, step_size, sample_rate):
    window = np.hamming(len(samples))
    fft_data = np.fft.fftshift(np.fft.fft(samples * window))
    power_db = 10 * np.log10(np.abs(fft_data) ** 2 + 1e-9)
    
    keep_ratio = step_size / sample_rate
    margin = int((1 - keep_ratio) / 2 * fft_size)
    return power_db[margin : fft_size - margin]

async def sdr_handler(websocket):
    logger.info(f"Klient ansluten {str(websocket.remote_address)}")
    try:
        while True:
            # 1. Kolla efter nya parametrar
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                data = json.loads(msg)
                if "fft_size" in data:
                    state["fft_size"] = int(data["fft_size"])
                if "step_size" in data:
                    state["step_size"] = float(data["step_size"])
                if "start" in data:
                    state["start"] = float(data["start"])
                if "stop" in data:
                    state["stop"] = float(data["stop"])
                if "paused" in data:
                    state["paused"] = bool(data["paused"])
                
                # Uppdatera SDR-hårdvaran om sample_rate ändras (valfritt)
                sdr.sample_rate = state["sample_rate"]
                logger.info(f"{str(websocket.remote_address)} Uppdaterad konfig: {state}")
            except: pass

            if state["paused"]:
                await asyncio.sleep(0.05)
                continue

            full_spectrum = []
            curr = state["start"]
            
            # 2. Scanning-loop
            while curr <= state["stop"]:
                sdr.center_freq = curr
                await asyncio.sleep(0.01) # Snabbare switch för live-känsla
                
                samples = sdr.read_samples(state["fft_size"])
                full_spectrum.append(get_clean_spectrum(
                    samples, state["fft_size"], state["step_size"], state["sample_rate"]
                ))
                curr += state["step_size"]
            
            if full_spectrum:
                combined = np.concatenate(full_spectrum)
                normalized = np.clip((combined + 60) * 3, 0, 255).astype(np.uint8)
                await websocket.send(normalized.tobytes())
                
    except Exception as e:
        logger.error(f"Fel: {e}")

async def main():
    async with websockets.serve(sdr_handler, "0.0.0.0", 8765):
        logger.info("Server redo...")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
