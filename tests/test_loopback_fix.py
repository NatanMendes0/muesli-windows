"""Loopback with a silence keeper: play zeros on the output device so the
loopback stream always has data, then verify TTS audio is captured."""
import subprocess
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

stop = threading.Event()

def silence_keeper():
    p = pyaudio.PyAudio()
    try:
        out = p.get_default_output_device_info()
        rate = int(out["defaultSampleRate"])
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=rate, output=True,
                        frames_per_buffer=int(rate * 0.1),
                        output_device_index=int(out["index"]))
        zeros = b"\x00" * (int(rate * 0.1) * 2)
        print("silence keeper running on", out["name"], flush=True)
        while not stop.is_set():
            stream.write(zeros)
        stream.stop_stream()
        stream.close()
    finally:
        p.terminate()

t = threading.Thread(target=silence_keeper, daemon=True)
t.start()
time.sleep(0.6)

p = pyaudio.PyAudio()
info = p.get_default_wasapi_loopback()
rate = int(info["defaultSampleRate"])
ch = min(2, int(info["maxInputChannels"]))
print(f"opening loopback rate={rate} ch={ch}", flush=True)
stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                frames_per_buffer=int(rate * 0.1), input_device_index=int(info["index"]))
print("reading 8s, TTS starts at ~1s...", flush=True)
proc = None
got_signal = False
for i in range(80):
    if i == 10:
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.Speak('The quick brown fox jumps over the lazy dog near the riverbank.')")
        proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = stream.read(int(rate * 0.1), exception_on_overflow=False)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    if rms > 0.005:
        got_signal = True
    if i % 10 == 0:
        print(f"  t={i * 0.1:3.1f}s rms={rms:.5f}", flush=True)
stop.set()
stream.stop_stream(); stream.close(); p.terminate()
if proc: proc.wait(timeout=30)
print("PASS" if got_signal else "FAIL (no signal captured)", flush=True)
