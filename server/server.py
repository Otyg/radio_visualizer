import sys
import types

# --- FIX 1: FEJKA PKG_RESOURCES (För Python 3.13+) ---
if 'pkg_resources' not in sys.modules:
    dummy_pkg = types.ModuleType('pkg_resources')
    dummy_pkg.get_distribution = lambda name: types.SimpleNamespace(version="0.3.0")
    sys.modules['pkg_resources'] = dummy_pkg

# --- FIX 2: ROBUST MONKEY PATCH FÖR DITHERING ---
import ctypes

# Vi definierar en tom funktion som matchar C-protokollet
def dummy_set_dithering(device, on):
    return 0

try:
    # Vi laddar modulen men hindrar den från att krascha vid uppslagning
    import rtlsdr.librtlsdr as librtlsdr_mod
    
    # Istället för att anropa attributet (vilket triggar felet), 
    # sätter vi det direkt i objektets dict om det saknas.
    setattr(librtlsdr_mod.librtlsdr, 'rtlsdr_set_dithering', dummy_set_dithering)
except Exception as e:
    print(f"Kunde inte applicera dithering-patch (kan ignoreras om import lyckas): {e}")

# --- NU IMPORTERAR VI RTLSDR ---
from rtlsdr import RtlSdr
import asyncio
import websockets
import numpy as np
import json

# --- SERVERLOGIK ---

sdr = RtlSdr()
# Använd en stabil samplingshastighet
SAMPLE_RATE = 2.4e6 
STEP_SIZE = 1.5e6    
sdr.sample_rate = SAMPLE_RATE
sdr.gain = 'auto'

FFT_SIZE = 1024 

def get_clean_spectrum(samples):
    """Beräknar FFT och klipper bort filterkanterna för snygg stitching"""
    # Applicera en Hamming-fönsterfunktion för att minska FFT-läckage
    window = np.hamming(len(samples))
    fft_data = np.fft.fftshift(np.fft.fft(samples * window))
    power_db = 10 * np.log10(np.abs(fft_data) ** 2 + 1e-9)
    
    # Beräkna marginaler för överlapp
    keep_ratio = STEP_SIZE / SAMPLE_RATE
    margin = int((1 - keep_ratio) / 2 * FFT_SIZE)
    return power_db[margin : FFT_SIZE - margin]

async def sdr_handler(websocket):
    # Standard: FM-bandet
    conf = {"start": 88e6, "stop": 108e6}
    print(f"Ny klient ansluten från {websocket.remote_address}")
    
    try:
        while True:
            # Kolla efter kontrollmeddelanden från klienten
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                data = json.loads(msg)
                if 'start' in data and 'stop' in data:
                    conf.update(data)
                    print(f"Uppdaterar scan: {conf['start']/1e6} - {conf['stop']/1e6} MHz")
            except (asyncio.TimeoutError, json.JSONDecodeError):
                pass 

            full_spectrum = []
            curr = conf["start"]
            
            # Utför wideband-scan genom att hoppa i frekvens
            while curr <= conf["stop"]:
                sdr.center_freq = curr
                # Vänta på att PLL stabiliseras (viktigt för rena mätningar)
                await asyncio.sleep(0.015) 
                
                samples = sdr.read_samples(FFT_SIZE)
                full_spectrum.append(get_clean_spectrum(samples))
                curr += STEP_SIZE
            
            if full_spectrum:
                # Sy ihop alla delar till en lång rad
                combined = np.concatenate(full_spectrum)
                # Normalisera värdena till 0-255 för att spara bandbredd (uint8)
                normalized = np.clip((combined + 60) * 3, 0, 255).astype(np.uint8)
                await websocket.send(normalized.tobytes())
                
    except websockets.exceptions.ConnectionClosed:
        print("Klient kopplade ifrån.")
    except Exception as e:
        print(f"Ett fel uppstod i loopen: {e}")

async def main():
    # Lyssna på alla gränssnitt (0.0.0.0) så att klienten kan ansluta utifrån
    async with websockets.serve(sdr_handler, "0.0.0.0", 8765):
        print("SDR-Server redo på ws://0.0.0.0:8765")
        await asyncio.Future() 

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStänger ner servern...")
        sdr.close()