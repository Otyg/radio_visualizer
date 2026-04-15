import asyncio
import websockets
import numpy as np
from rtlsdr import RtlSdr
import json

sdr = RtlSdr()
SAMPLE_RATE = 2.4e6  # Vi läser 2.4 MHz
STEP_SIZE = 1.5e6    # Men vi flyttar oss bara 1.5 MHz (ger överlapp)
sdr.sample_rate = SAMPLE_RATE
sdr.gain = 'auto'

FFT_SIZE = 1024 # Upplösning per läsning

def get_clean_spectrum(samples):
    """Räknar ut FFT och kastar kanterna för att slippa filter-roll-off"""
    fft_data = np.fft.fftshift(np.fft.fft(samples))
    power_db = 10 * np.log10(np.abs(fft_data) ** 2 + 1e-9)
    
    # Beräkna hur många pixlar vi ska behålla baserat på STEP_SIZE
    keep_ratio = STEP_SIZE / SAMPLE_RATE
    margin = int((1 - keep_ratio) / 2 * FFT_SIZE)
    
    # Returnera bara den rena mittendelen
    return power_db[margin : FFT_SIZE - margin]

async def sdr_handler(websocket):
    conf = {"start": 88e6, "stop": 108e6}
    try:
        while True:
            # Kolla efter kommandon (non-blocking)
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                data = json.loads(msg)
                conf.update(data)
            except: pass

            full_data = []
            curr = conf["start"]
            
            while curr <= conf["stop"]:
                sdr.center_freq = curr
                await asyncio.sleep(0.02) # PLL-stabilisering
                
                samples = sdr.read_samples(FFT_SIZE)
                clean_segment = get_clean_spectrum(samples)
                full_data.append(clean_segment)
                
                curr += STEP_SIZE
            
            if full_data:
                spectrum = np.concatenate(full_data)
                # Normalisera till 0-255 (uint8) för nätverkstransfer
                normalized = np.clip((spectrum + 60) * 3, 0, 255).astype(np.uint8)
                await websocket.send(normalized.tobytes())
                
    except websockets.exceptions.ConnectionClosed: pass

async def main():
    async with websockets.serve(sdr_handler, "0.0.0.0", 8765):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())