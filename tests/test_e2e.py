"""End to end self test: TTS through the speakers -> loopback capture -> Whisper -> enhance.

Run:  .venv\\Scripts\\python.exe tests\\test_e2e.py
Plays audio out loud through the default output device.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from muesli import audio, db, enhance, settings, stt, templates  # noqa: E402

SPOKEN = ("Welcome to the quarterly planning meeting. "
          "We decided to launch the new dashboard in March. "
          "Sarah will send the pricing proposal to the client by Friday. "
          "The main concern is the migration timeline for the database. "
          "Let's schedule a follow up call next Tuesday to review the budget.")

KEYWORDS = ["dashboard", "pricing", "migration", "budget"]


def make_tts_wav(text: str, path: str):
    """Synthesize speech to a WAV file with Windows SAPI (works headless)."""
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.SetOutputToWaveFile('{}'); $s.Rate = 0; $s.Speak('{}'); $s.Dispose()"
          .format(path, text.replace("'", "''")))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def play_wav(path: str):
    """Play a WAV through the default output device so loopback can capture it."""
    import wave
    import pyaudiowpatch as pyaudio
    wf = wave.open(path, "rb")
    p = pyaudio.PyAudio()
    out = p.get_default_output_device_info()
    stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(), rate=wf.getframerate(),
                    output=True, output_device_index=int(out["index"]))
    data = wf.readframes(2048)
    while data:
        stream.write(data)
        data = wf.readframes(2048)
    stream.stop_stream()
    stream.close()
    p.terminate()
    wf.close()


def main():
    print("== Muesli end to end self test ==")
    db.init()

    print("[1/5] Preloading Whisper model (downloads on first run)...")
    t0 = time.time()
    stt._get_local_model()
    print(f"      model ready in {time.time() - t0:.1f}s")

    print("[2/5] Starting capture (mic + loopback)...")
    cap = audio.CaptureSession()
    cap.start()
    if cap.warnings:
        print("      warnings:", cap.warnings)

    segments = []
    def on_segment(t0_, t1_, speaker, text):
        segments.append({"t0": t0_, "t1": t1_, "speaker": speaker, "text": text})
        print(f"      [{t0_:6.1f}-{t1_:6.1f}] {speaker or '?':4s} {text}")

    tr = stt.StreamingTranscriber(cap, on_segment, lambda s: s and print("      status:", s))
    tr.start()

    print("[3/5] Speaking test script through the speakers...")
    wav_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_test.wav")
    make_tts_wav(SPOKEN, wav_path)
    play_wav(wav_path)
    print("      speech done, letting the transcriber drain...")
    time.sleep(4)
    cap.stop()
    tr.stop_flag.set()
    tr.join(timeout=120)

    full = " ".join(s["text"] for s in segments).lower()
    print(f"\n[4/5] Transcript ({len(segments)} segments):\n      {full[:500]}")
    hits = [k for k in KEYWORDS if k in full]
    print(f"      keyword hits: {hits} ({len(hits)}/{len(KEYWORDS)})")

    print("[5/5] Enhancement...")
    meeting = {"title": "Self test meeting",
               "notes_md": "- pricing concerns\n- migration timeline\n- follow up call"}
    tpl = templates.BUILTIN[0]
    md_ex = enhance.extractive_enhance(meeting, segments, tpl)
    print("      extractive output length:", len(md_ex))
    prov = enhance.llm_available()
    print("      llm available:", prov)
    md_llm, provider = "", ""
    if prov.get("provider"):
        t0 = time.time()
        md_llm, provider = enhance.enhance(meeting, segments, tpl)
        print(f"      llm enhance via {provider} in {time.time() - t0:.1f}s, "
              f"length {len(md_llm)}")

    ok_capture = len(segments) > 0
    ok_keywords = len(hits) >= 2
    ok_extractive = len(md_ex) > 100
    print("\n== RESULTS ==")
    print("capture+stt segments :", "PASS" if ok_capture else "FAIL")
    print("keyword accuracy     :", "PASS" if ok_keywords else "FAIL", hits)
    print("extractive enhance   :", "PASS" if ok_extractive else "FAIL")
    if md_llm:
        print("llm enhance          : PASS (" + provider + ")")
        print("\n--- LLM output preview ---\n" + md_llm[:900])
    print("\n--- Extractive output preview ---\n" + md_ex[:700])
    sys.exit(0 if (ok_capture and ok_keywords and ok_extractive) else 1)


if __name__ == "__main__":
    main()
