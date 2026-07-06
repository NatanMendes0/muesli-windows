"""Minimal raw PyAudioWPatch test. Arg: 'mic' or 'loop'. No threads, one stream."""
import sys
import numpy as np
import pyaudiowpatch as pyaudio

which = sys.argv[1] if len(sys.argv) > 1 else "loop"
p = pyaudio.PyAudio()
if which == "mic":
    info = p.get_default_input_device_info()
else:
    info = p.get_default_wasapi_loopback()
rate = int(info["defaultSampleRate"])
ch = min(2, int(info["maxInputChannels"]))
print(f"opening {which}: index={info['index']} name={info['name']!r} rate={rate} ch={ch}", flush=True)
stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                frames_per_buffer=int(rate * 0.1), input_device_index=int(info["index"]))
print("opened, reading 3s...", flush=True)
for i in range(30):
    raw = stream.read(int(rate * 0.1), exception_on_overflow=False)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if i % 10 == 0:
        print(f"  block {i} rms={float(np.sqrt(np.mean(pcm**2))):.5f}", flush=True)
stream.stop_stream()
stream.close()
p.terminate()
print("PASS", flush=True)
