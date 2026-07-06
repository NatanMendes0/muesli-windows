"""Capture-only isolation test: no Whisper, just mic + loopback for 8 seconds."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from muesli import audio

def speak_async(text):
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.Speak('{}')".format(text))
    return subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    print("capture only test", flush=True)
    cap = audio.CaptureSession()
    cap.start()
    print("started, warnings:", cap.warnings, flush=True)
    proc = speak_async("Testing one two three. The quick brown fox jumps over the lazy dog.")
    total = np.zeros(0, dtype=np.float32)
    for i in range(16):
        time.sleep(0.5)
        chunk = cap.pull_mixed()
        total = np.concatenate([total, chunk])
        print(f"  t={i * 0.5 + 0.5:4.1f}s pulled={chunk.size:6d} totals={cap.totals} "
              f"levels mic={cap.levels['mic']:.4f} loop={cap.levels['loop']:.4f}", flush=True)
    proc.terminate()
    cap.stop()
    rms = float(np.sqrt(np.mean(total ** 2))) if total.size else 0.0
    print(f"done. mixed samples={total.size} rms={rms:.5f}", flush=True)
    print("PASS" if total.size > audio.TARGET_RATE * 4 and rms > 0.001 else "FAIL", flush=True)

if __name__ == "__main__":
    main()
