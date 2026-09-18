#!/usr/bin/env python3
"""
J.A.R.V.I.S. Local Physical Microphone Wake-Word Listener (High-Speed Engine)
Monitors the laptop's physical microphone using PipeWire streaming PCM.
Detects voice activity in real time, checks for 'Hey Jarvis' or 'Jarvis' via
Groq Whisper Turbo in-memory, executes commands via the local hub, and plays
instant low-latency neural speech confirmations.
"""

import os
import sys
import time
import re
import io
import wave
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import httpx

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
HUB_CHAT_URL = "http://127.0.0.1:8765/api/chat"

AUDIO_CACHE_DIR = Path.home() / ".jarvis" / "audio_cache"
CHIME_PING = AUDIO_CACHE_DIR / "chime_ping.wav"
ACK_YES_SIR = AUDIO_CACHE_DIR / "ack_yes_sir.mp3"

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 50
FRAME_SIZE = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0) * 2)  # 1600 bytes
ENERGY_THRESHOLD = 450.0


def play_sound(file_path: Path | str, async_play: bool = False):
    """Play audio file via PipeWire or PulseAudio."""
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not player or not Path(file_path).exists():
        return

    if async_play:
        subprocess.Popen([player, str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run([player, str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(text: str):
    """Play TTS speech with instant audio caching."""
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not player:
        return

    # Check for pre-cached phrases
    text_clean = text.strip().lower()
    if "yes, sir" in text_clean and ACK_YES_SIR.exists():
        play_sound(ACK_YES_SIR, async_play=False)
        return

    # Call local hub TTS endpoint for dynamic phrases
    try:
        import urllib.parse
        encoded = urllib.parse.quote(text)
        url = f"http://127.0.0.1:8765/api/tts?text={encoded}"
        tmp_audio = f"/dev/shm/jarvis_listener_reply_{os.getpid()}.mp3"
        subprocess.run(["curl", "-s", "-o", tmp_audio, url], timeout=8)
        if os.path.exists(tmp_audio) and os.path.getsize(tmp_audio) > 100:
            play_sound(tmp_audio, async_play=False)
            try:
                os.remove(tmp_audio)
            except Exception:
                pass
            return
    except Exception:
        pass

HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(8.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
)


def transcribe_pcm(pcm_bytes: bytes) -> str:
    """Transcribe in-memory PCM bytes via Groq Whisper Turbo."""
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": ("audio.wav", wav_buf.getvalue(), "audio/wav")}
    data = {
        "model": "whisper-large-v3-turbo",
        "language": "en",
        "prompt": "Hey Jarvis"
    }

    try:
        resp = HTTP_CLIENT.post(GROQ_WHISPER_URL, headers=headers, files=files, data=data)
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
    except Exception:
        pass
    return ""


def open_jarvis_terminal(cmd: str = ""):
    """Ensure J.A.R.V.I.S. terminal window pops up on the Linux desktop."""
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if "WAYLAND_DISPLAY" not in env:
        env["WAYLAND_DISPLAY"] = "wayland-0"

    try:
        check = subprocess.run(["pgrep", "-f", "python.*/jarvis.py$"], capture_output=True, text=True)
        pids = [p for p in check.stdout.strip().splitlines() if p and int(p) != os.getpid()]
        if pids:
            subprocess.run(["notify-send", "-i", "utilities-terminal", "J.A.R.V.I.S.", "Session active. Processing command..."], env=env)
            return
    except Exception:
        pass

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
    """Send voice command to J.A.R.V.I.S. hub and respond purely in the background."""
    print(f"\n✦ [HEY JARVIS TRIGGERED]: \"{cmd}\"")

    try:
        resp = HTTP_CLIENT.post(HUB_CHAT_URL, json={"prompt": cmd})
        if resp.status_code == 200:
            data = resp.json()
            spoken = data.get("spoken", "Completed, Sir.")
            print(f"✦ J.A.R.V.I.S. Response: {spoken}")
            subprocess.run(["notify-send", "-i", "audio-input-microphone", "J.A.R.V.I.S.", spoken], capture_output=True)
            speak(spoken)
        else:
            speak("I encountered an issue contacting the hub, Sir.")
    except Exception as e:
        print(f"Hub request error: {e}")
        speak("Unable to reach the local hub, Sir.")


def listen_loop():
    """High-speed streaming microphone monitoring loop."""
    print("═" * 58)
    print("✦ J.A.R.V.I.S. HIGH-SPEED PHYSICAL MICROPHONE LISTENER")
    print("═" * 58)
    print("Monitoring PipeWire audio stream in real time...")
    print("Say \"Hey Jarvis <command>\" or \"Jarvis <command>\"...")
    print("Press Ctrl+C to terminate listener.\n")

    try:
        proc = subprocess.Popen(
            ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"Failed to start PipeWire recording: {e}")
        return

    buffered_pcm = bytearray()
    is_speaking = False
    silence_frames = 0
    max_silence_frames = 9  # ~450ms silence cuts turn
    speech_frames = 0

    try:
        while True:
            chunk = proc.stdout.read(FRAME_SIZE)
            if not chunk:
                break

            arr = np.frombuffer(chunk, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))

            if rms > ENERGY_THRESHOLD:
                speech_frames += 1
                silence_frames = 0
                if speech_frames >= 2:
                    is_speaking = True
                buffered_pcm.extend(chunk)
            else:
                if is_speaking:
                    buffered_pcm.extend(chunk)
                    silence_frames += 1
                    if silence_frames >= max_silence_frames:
                        # Spoken phrase ended! Transcribe in-memory
                        if len(buffered_pcm) > FRAME_SIZE * 4:
                            text = transcribe_pcm(bytes(buffered_pcm))
                            if text:
                                wake_pattern = r"\b(hey\s+jarvis|jarvis)\b"
                                match = re.search(wake_pattern, text, re.IGNORECASE)
                                if match:
                                    clean_cmd = text[match.end():].strip().lstrip(",:;- ")
                                    # Play instant acoustic ping cue
                                    if CHIME_PING.exists():
                                        play_sound(CHIME_PING, async_play=True)

                                    # Respond purely in background via audio & desktop notification
                                    if not clean_cmd:
                                        subprocess.run(["notify-send", "-i", "audio-input-microphone", "J.A.R.V.I.S.", "Wake-word acknowledged. Standing by, Sir."], capture_output=True)
                                        speak("Yes, Sir? How may I assist you?")
                                    else:
                                        process_command(clean_cmd)

                        # Reset state for next phrase
                        buffered_pcm.clear()
                        is_speaking = False
                        silence_frames = 0
                        speech_frames = 0
                else:
                    speech_frames = 0
                    # Keep rolling 150ms buffer
                    if len(buffered_pcm) > FRAME_SIZE * 3:
                        buffered_pcm = buffered_pcm[-FRAME_SIZE * 3:]
                    buffered_pcm.extend(chunk)

    except KeyboardInterrupt:
        print("\nShutting down high-speed microphone listener.")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    listen_loop()
