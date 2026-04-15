import sys
import types

# --- FIX 1: FEJKA PKG_RESOURCES (För Python 3.13+) ---
# Vi skapar en tom modul i minnet så att 'import pkg_resources' inte kraschar
if 'pkg_resources' not in sys.modules:
    dummy_pkg = types.ModuleType('pkg_resources')
    # Lägg till de funktioner som pyrtlsdr förväntar sig
    dummy_pkg.get_distribution = lambda name: types.SimpleNamespace(version="0.3.0")
    sys.modules['pkg_resources'] = dummy_pkg

# --- FIX 2: MONKEY PATCH DITHERING ---
import ctypes

# För att kunna patcha librtlsdr måste vi ladda in modulen i rätt ordning
try:
    # Vi försöker ladda in biblioteket manuellt först
    import rtlsdr.librtlsdr as librtlsdr_module
    def dummy_set_dithering(device, on): return 0
    librtlsdr_module.librtlsdr.rtlsdr_set_dithering = dummy_set_dithering
except ImportError:
    # Om vi inte kan importera den än, fortsätt och hoppas på det bästa
    pass

# --- NU KAN VI IMPORTERA RTLSDR ---
import asyncio
import websockets
import numpy as np
from rtlsdr import RtlSdr
import json

# --- DIN BEFINTLIGA SERVERKOD ---
sdr = RtlSdr()
SAMPLE_RATE = 2.4e6
STEP_SIZE = 1.5e6
sdr.sample_rate = SAMPLE_RATE
sdr.gain = 'auto'
FFT_SIZE = 1024

def get_clean_spectrum(samples):
    fft_data = np.fft.fftshift(np.fft.fft(samples))
    power_db = 10 * np.log10(np.abs(fft_data) ** 2 + 1e-9)
    keep_ratio = STEP_SIZE / SAMPLE_RATE
    margin = int((1 - keep_ratio) / 2 * FFT_SIZE)
    return power_db[margin : FFT_SIZE - margin]

async def sdr_handler(websocket):
    conf = {"start": 88e6, "stop": 108e6}
    print("Klient ansluten!")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                data = json.loads(msg)
                conf.update(data)
            except: pass

            full_data = []
            curr = conf["start"]
            while curr <= conf["stop"]:
                sdr.center_freq = curr
                await asyncio.sleep(0.02)
                samples = sdr.read_samples(FFT_SIZE)
                full_data.append(get_clean_spectrum(samples))
                curr += STEP_SIZE
            
            if full_data:
                spectrum = np.concatenate(full_data)
                normalized = np.clip((spectrum + 60) * 3, 0, 255).astype(np.uint8)
                await websocket.send(normalized.tobytes())
    except Exception as e:
        print(f"Fel: {e}")

async def main():
    async with websockets.serve(sdr_handler, "0.0.0.0", 8765):
        print("Server startad på port 8765...")
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sdr.close()