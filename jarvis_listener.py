#!/usr/bin/env python3
"""
J.A.R.V.I.S. Local Physical Microphone Wake-Word Listener
Monitors the laptop's physical microphone using PipeWire (pw-record).
Detects voice activity, checks for 'Hey Jarvis' or 'Jarvis' via Groq Whisper API,
executes the command via the J.A.R.V.I.S. hub, and announces results via speakers.
"""

import os
import sys
import time
import re
import wave
import math
import shutil
import subprocess
from pathlib import Path

import httpx

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
HUB_CHAT_URL = "http://127.0.0.1:8765/api/chat"

TEMP_WAV = "/tmp/jarvis_mic_chunk.wav"
ENERGY_THRESHOLD = 500  # Voice activity threshold


def calculate_rms(wav_path: str) -> float:
    """Calculate RMS volume level of recorded WAV file."""
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            if not frames:
                return 0.0
            # Calculate RMS energy across 16-bit PCM
            count = len(frames) // 2
            sum_squares = 0.0
            for i in range(0, len(frames), 2):
                val = int.from_bytes(frames[i:i+2], byteorder="little", signed=True)
                sum_squares += val * val
            rms = math.sqrt(sum_squares / count) if count > 0 else 0
            return rms
    except Exception:
        return 0.0


def transcribe_chunk(wav_path: str) -> str:
    """Transcribe audio chunk via Groq Whisper API."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(wav_path, "rb") as f:
            files = {"file": ("audio.wav", f, "audio/wav")}
            data = {"model": "whisper-large-v3-turbo", "language": "en"}
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(GROQ_WHISPER_URL, headers=headers, files=files, data=data)
                if resp.status_code == 200:
                    return resp.json().get("text", "").strip()
    except Exception:
        pass
    return ""


def speak(text: str):
    """Play TTS speech via PipeWire."""
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not player:
        return

    # Call local hub TTS endpoint to fetch MP3
    try:
        import urllib.parse
        encoded = urllib.parse.quote(text)
        url = f"http://127.0.0.1:8765/api/tts?text={encoded}"
        tmp_audio = "/tmp/jarvis_reply.mp3"
        subprocess.run(["curl", "-s", "-o", tmp_audio, url], timeout=10)
        if os.path.exists(tmp_audio) and os.path.getsize(tmp_audio) > 100:
            subprocess.run([player, tmp_audio], capture_output=True)
    except Exception:
        # Fallback to local speech-dispatcher
        subprocess.run(["spd-say", "-r", "10", text], capture_output=True)


def open_jarvis_terminal(cmd: str = ""):
    """Ensure J.A.R.V.I.S. terminal window pops up on the Linux desktop."""
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if "WAYLAND_DISPLAY" not in env:
        env["WAYLAND_DISPLAY"] = "wayland-0"

    # Check if jarvis interactive terminal is already running
    try:
        check = subprocess.run(["pgrep", "-f", "python.*/jarvis.py$"], capture_output=True, text=True)
        pids = [p for p in check.stdout.strip().splitlines() if p and int(p) != os.getpid()]
        if pids:
            subprocess.run(["notify-send", "-i", "utilities-terminal", "J.A.R.V.I.S.", "Session active. Processing command..."], env=env)
            return
    except Exception:
        pass

    # Launch dedicated J.A.R.V.I.S. terminal window
    try:
        terminal_cmd = [
            "gnome-terminal",
            "--title=✦ J.A.R.V.I.S. AI TERMINAL",
            "--",
            "/home/feds/.local/bin/jarvis"
        ]
        subprocess.Popen(terminal_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Failed to launch terminal: {e}")


def process_command(cmd: str):
    """Send voice command to J.A.R.V.I.S. hub."""
    print(f"\n✦ [HEY JARVIS TRIGGERED]: \"{cmd}\"")
    subprocess.run(["notify-send", "-i", "audio-input-microphone", "J.A.R.V.I.S.", f"Heard: {cmd}"], capture_output=True)

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(HUB_CHAT_URL, json={"prompt": cmd})
            if resp.status_code == 200:
                data = resp.json()
                spoken = data.get("spoken", "Completed, Sir.")
                print(f"✦ J.A.R.V.I.S. Response: {spoken}")
                speak(spoken)
            else:
                speak("I encountered an issue contacting the hub, Sir.")
    except Exception as e:
        print(f"Hub request error: {e}")
        speak("Unable to reach the local hub, Sir.")


def listen_loop():
    """Main continuous microphone monitoring loop."""
    print("═" * 58)
    print("✦ J.A.R.V.I.S. PHYSICAL MICROPHONE LISTENER ACTIVE")
    print("═" * 58)
    print("Monitoring microphone via PipeWire...")
    print("Say \"Hey Jarvis <command>\" or \"Jarvis <command>\"...")
    print("Press Ctrl+C to terminate listener.\n")

    while True:
        try:
            # Record 3.0 second chunk
            cmd = ["pw-record", "--rate", "16000", "--channels", "1", TEMP_WAV]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3.0)
            proc.terminate()
            proc.wait(timeout=1)

            if not os.path.exists(TEMP_WAV):
                continue

            # Measure volume level
            rms = calculate_rms(TEMP_WAV)
            if rms < ENERGY_THRESHOLD:
                # Silence / quiet ambient noise, skip API call
                continue

            # Voice activity detected, transcribe
            text = transcribe_chunk(TEMP_WAV)
            if not text:
                continue

            # Check for wake words
            wake_pattern = r"\b(hey\s+jarvis|jarvis)\b"
            match = re.search(wake_pattern, text, re.IGNORECASE)
            if match:
                clean_cmd = text[match.end():].strip().lstrip(",:;- ")
                # Automatically pop open the J.A.R.V.I.S. terminal on desktop!
                open_jarvis_terminal(cmd=clean_cmd)
                if not clean_cmd:
                    speak("Yes, Sir? How may I assist you?")
                else:
                    process_command(clean_cmd)

        except KeyboardInterrupt:
            print("\nShutting down microphone listener.")
            break
        except Exception as e:
            time.sleep(1)


if __name__ == "__main__":
    listen_loop()
