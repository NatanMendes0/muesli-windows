"""Play a 440 Hz tone from this process and confirm the loopback stream hears it."""
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

stop = threading.Event()

def player():
    p = pyaudio.PyAudio()
    try:
        out = p.get_default_output_device_info()
        rate = int(out["defaultSampleRate"])
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=rate, output=True,
                        frames_per_buffer=int(rate * 0.1),
                        output_device_index=int(out["index"]))
        t = 0
        n = int(rate * 0.1)
        while not stop.is_set():
            idx = np.arange(t, t + n)
            tone = (0.2 * np.sin(2 * np.pi * 440 * idx / rate) * 32767).astype(np.int16)
            stream.write(tone.tobytes())
            t += n
        stream.stop_stream(); stream.close()
    finally:
        p.terminate()

threading.Thread(target=player, daemon=True).start()
time.sleep(0.8)

p = pyaudio.PyAudio()
info = p.get_default_wasapi_loopback()
rate = int(info["defaultSampleRate"])
ch = min(2, int(info["maxInputChannels"]))
stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                frames_per_buffer=int(rate * 0.1), input_device_index=int(info["index"]))
peak = 0.0
for i in range(40):
    raw = stream.read(int(rate * 0.1), exception_on_overflow=False)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    peak = max(peak, rms)
    if i % 10 == 0:
        print(f"  t={i * 0.1:3.1f}s rms={rms:.5f}", flush=True)
stop.set()
stream.stop_stream(); stream.close(); p.terminate()
print(f"peak rms={peak:.5f}", flush=True)
print("PASS" if peak > 0.02 else "FAIL", flush=True)
