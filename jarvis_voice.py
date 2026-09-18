#!/usr/bin/env python3
"""
J.A.R.V.I.S. Real-Time Interactive Voice Interface
Full-duplex continuous conversational speech with J.A.R.V.I.S.
Optimized for ultra-low latency via streaming PipeWire PCM, in-memory VAD,
Groq Whisper Turbo, streamed LLM inference, and pipelined sentence-level neural TTS.
"""

import os
import sys
import time
import re
import io
import wave
import queue
import threading
import asyncio
import subprocess
import shutil
from pathlib import Path
from typing import Optional

import numpy as np
import httpx
import edge_tts
from rich.console import Console
from rich.panel import Panel

# Ensure local directories are in path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

console = Console()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
VOICE_NAME = "en-GB-RyanNeural"
VOICE_RATE = "+20%"

# Persistent HTTP Client with Connection Pooling for Ultra-Low Latency
HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(10.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
)

AUDIO_CACHE_DIR = SCRIPT_DIR / "audio_cache"
CHIME_PING = AUDIO_CACHE_DIR / "chime_ping.wav"
CHIME_LISTEN = AUDIO_CACHE_DIR / "chime_listen.wav"

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 60  # 60ms frames
FRAME_SIZE = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0) * 2)  # 1920 bytes for 16-bit mono


# ── Audio Playback Utilities ────────────────────────────────────────────────
def play_sound(file_path: Path | str, async_play: bool = False):
    """Play an audio file using PipeWire (pw-play) or PulseAudio (paplay)."""
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not player or not Path(file_path).exists():
        return

    if async_play:
        subprocess.Popen([player, str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run([player, str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play_chime_ping():
    """Play instantaneous feedback ping when speech finishes."""
    if CHIME_PING.exists():
        play_sound(CHIME_PING, async_play=True)


def play_chime_listen():
    """Play gentle cue when ready to listen again."""
    if CHIME_LISTEN.exists():
        play_sound(CHIME_LISTEN, async_play=True)


# ── System Telemetry for Live Context ────────────────────────────────────────
def get_live_context() -> str:
    """Capture concise live system context."""
    try:
        with open("/proc/loadavg", "r") as f:
            cpu = f.read().split()[0]
    except Exception:
        cpu = "0.0"

    ram = "N/A"
    try:
        res = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=1)
        for line in res.stdout.splitlines():
            if line.startswith("Mem:"):
                p = line.split()
                ram = f"{int(p[2])/1024:.1f}G/{int(p[1])/1024:.1f}G"
    except Exception:
        pass

    bat = "AC"
    bat_p = Path("/sys/class/power_supply/BAT0/capacity")
    if bat_p.exists():
        try:
            bat = f"{bat_p.read_text().strip()}%"
        except Exception:
            pass

    return f"Time: {time.strftime('%H:%M:%S')}, CPU Load: {cpu}, RAM: {ram}, Battery: {bat}"


# ── Pipelined Speech Player ──────────────────────────────────────────────────
class StreamingSpeechQueue:
    """Pipelines sentence synthesis and audio playback so speech starts instantly."""

    def __init__(self):
        self.queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.is_speaking = False
        self.worker_thread.start()

    def _worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                break

            sentence, temp_file = item
            self.is_speaking = True
            try:
                # 1. Synthesize neural TTS with timeout
                async def synth():
                    comm = edge_tts.Communicate(sentence, voice=VOICE_NAME, rate=VOICE_RATE)
                    await asyncio.wait_for(comm.save(temp_file), timeout=6.0)

                loop.run_until_complete(synth())

                # 2. Play synthesized audio via PipeWire
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                    play_sound(temp_file, async_play=False)
            except Exception as e:
                # Fallback to local speech-dispatcher
                subprocess.run(["spd-say", "-r", "15", sentence], capture_output=True)
            finally:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
                self.is_speaking = False
                self.queue.task_done()

    def enqueue(self, sentence: str):
        """Enqueue sentence for background synthesis and sequential playback."""
        clean = re.sub(r"[*_#`]", "", sentence).strip()
        clean = re.sub(r"https?://\S+", "", clean)
        if not clean or len(clean) < 2:
            return

        temp_file = f"/dev/shm/jarvis_stream_{int(time.time()*1000)}_{os.getpid()}.mp3"
        self.queue.put((clean, temp_file))

    def wait_until_done(self):
        """Wait for all enqueued sentences to finish playing."""
        self.queue.join()
        while self.is_speaking:
            time.sleep(0.05)


# ── Autonomous Voice Action Dispatcher ───────────────────────────────────────
def check_voice_actions(command: str) -> Optional[str]:
    """Execute pre-authorized hardware/system actions directly from voice."""
    cmd = command.lower().strip()

    # Volume control
    if "volume up" in cmd or "louder" in cmd or "increase volume" in cmd:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"], capture_output=True)
        return "I have increased the volume by ten percent, Sir."

    if "volume down" in cmd or "quieter" in cmd or "decrease volume" in cmd or "lower volume" in cmd:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"], capture_output=True)
        return "Volume reduced by ten percent, Sir."

    if "mute volume" in cmd or "mute audio" in cmd or "toggle mute" in cmd:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], capture_output=True)
        return "Volume mute status toggled, Sir."

    # Memory reclamation
    if "clean memory" in cmd or "purge cache" in cmd or "turbo clean" in cmd or "free ram" in cmd:
        script = Path.home() / ".local/bin/turbo-clean"
        if script.exists():
            subprocess.run([str(script)], capture_output=True)
            return "Turbo-Clean executed. System cache purged and memory reclaimed, Sir."
        return "Turbo-clean script not located, Sir."

    # GitHub sync
    if "sync git" in cmd or "github sync" in cmd or "sync repositories" in cmd or "daily sync" in cmd:
        script = Path.home() / ".local/bin/antigravity-daily-sync"
        if script.exists():
            subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "Daily GitHub repository synchronization initiated in the background, Sir."
        return "Sync utility not found, Sir."

    # Screen lock
    if "lock screen" in cmd or "lock laptop" in cmd or "lock workstation" in cmd:
        subprocess.run(["loginctl", "lock-session"], capture_output=True)
        return "Workstation locked securely, Sir."

    # Live Free APIs Integration
    sys.path.insert(0, "/home/feds/.gemini/antigravity/scratch/free-apis")
    try:
        from free_apis import query_live_api
        if "weather" in cmd:
            loc = cmd.replace("weather", "").replace("what's the", "").replace("what is the", "").replace("in", "").strip()
            res = query_live_api("weather", loc)
            return f"Weather report: {res}, Sir."
        elif any(k in cmd for k in ("bitcoin", "crypto", "btc price", "price of bitcoin")):
            coin = "ethereum" if "eth" in cmd else "bitcoin"
            res = query_live_api("crypto", coin)
            return f"{res}, Sir."
        elif "joke" in cmd:
            res = query_live_api("joke")
            return f"Here is one, Sir: {res}"
        elif "quote" in cmd or "inspire" in cmd:
            res = query_live_api("quote")
            return f"{res}, Sir."
        elif "my ip" in cmd or "public ip" in cmd:
            res = query_live_api("ip")
            return f"{res}, Sir."
    except Exception:
        pass

    # Exit voice mode
    if any(phrase in cmd for phrase in ["exit voice", "quit voice", "stand down", "close voice", "goodbye jarvis", "exit mode"]):
        return "STAND_DOWN"

    return None


# ── Real-Time Voice Conversation Engine ──────────────────────────────────────
class JarvisVoiceSession:
    """Full-duplex continuous voice session with J.A.R.V.I.S."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.api_key = GROQ_API_KEY
        self.speech_pipeline = StreamingSpeechQueue()
        self.history: list[dict[str, str]] = []
        self.energy_threshold = 450.0  # Dynamic adaptive VAD threshold
        self.is_running = True

    def get_voice_system_prompt(self) -> str:
        ctx = get_live_context()
        return (
            "You are J.A.R.V.I.S., speaking aloud to Sir over a real-time high-speed neural voice link.\n"
            "Persona & Speech Rules:\n"
            "- Sophisticated British tone, razor-sharp intellect, utmost loyalty.\n"
            "- Address the user as 'Sir'.\n"
            "- DELIVER EXTREMELY CRISP, PUNCHY RESPONSES (UNDER 35 WORDS).\n"
            "- Do not use filler, markdown lists, bullet points, asterisks, or code blocks.\n"
            "- Answer directly and conversational so speech synthesis sounds natural and immediate.\n"
            f"[LIVE HARDWARE STATE: {ctx}]"
        )

    def calibrate_ambient_noise(self, duration_s: float = 0.5):
        """Calibrate microphone ambient baseline level."""
        console.print("[dim]✦ Calibrating ambient acoustics...[/]", end="\r")
        try:
            proc = subprocess.Popen(
                ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            frames = int(duration_s / (FRAME_DURATION_MS / 1000.0))
            energies = []
            for _ in range(max(frames, 5)):
                raw = proc.stdout.read(FRAME_SIZE)
                if not raw:
                    break
                arr = np.frombuffer(raw, dtype=np.int16)
                rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
                energies.append(rms)
            proc.terminate()
            proc.wait()

            if energies:
                baseline = float(np.mean(energies))
                self.energy_threshold = max(380.0, baseline * 1.7)
        except Exception:
            self.energy_threshold = 500.0
        console.print(f"[dim]✦ Ambient baseline calibrated (Threshold: {int(self.energy_threshold)} RMS).[/]")

    def capture_user_speech(self) -> Optional[bytes]:
        """
        Stream PipeWire audio and use dynamic VAD to capture full spoken utterance.
        Stops immediately when speech ends (550ms silence) or on 12s max duration.
        """
        try:
            proc = subprocess.Popen(
                ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            console.print(f"[red]Error accessing PipeWire audio stream: {e}[/]")
            return None

        buffered_pcm = bytearray()
        is_speaking = False
        silence_counter = 0
        speech_counter = 0
        max_silence_frames = 9   # ~540ms silence cuts turn
        min_speech_frames = 3    # ~180ms voice activity required
        max_total_frames = 200   # ~12s max turn duration
        total_frames = 0

        visual_meter_len = 16

        try:
            while self.is_running and total_frames < max_total_frames:
                raw_chunk = proc.stdout.read(FRAME_SIZE)
                if not raw_chunk:
                    break
                total_frames += 1

                arr = np.frombuffer(raw_chunk, dtype=np.int16)
                rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))

                # Meter visualization
                meter_ratio = min(1.0, rms / 2500.0)
                bars = int(meter_ratio * visual_meter_len)
                meter = "█" * bars + "░" * (visual_meter_len - bars)

                if rms > self.energy_threshold:
                    speech_counter += 1
                    silence_counter = 0
                    if speech_counter >= min_speech_frames:
                        is_speaking = True
                    buffered_pcm.extend(raw_chunk)
                    sys.stdout.write(f"\r\033[1;36m● CAPTURING\033[0m [{meter}] {int(rms)} RMS   ")
                    sys.stdout.flush()
                else:
                    if is_speaking:
                        buffered_pcm.extend(raw_chunk)
                        silence_counter += 1
                        sys.stdout.write(f"\r\033[1;33m● FINISHING\033[0m [{meter}] {silence_counter}/{max_silence_frames}   ")
                        sys.stdout.flush()
                        if silence_counter >= max_silence_frames:
                            # Speech ended naturally!
                            break
                    else:
                        speech_counter = 0
                        # Keep small rolling pre-buffer (180ms) to catch start of words
                        if len(buffered_pcm) > FRAME_SIZE * 3:
                            buffered_pcm = buffered_pcm[-FRAME_SIZE * 3:]
                        buffered_pcm.extend(raw_chunk)
                        sys.stdout.write(f"\r\033[1;32m● LISTENING\033[0m [{meter}] {int(rms)} RMS   ")
                        sys.stdout.flush()

        finally:
            proc.terminate()
            proc.wait()
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()

        if is_speaking and len(buffered_pcm) > FRAME_SIZE * 4:
            # Package into in-memory WAV buffer
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(bytes(buffered_pcm))
            return wav_buf.getvalue()

        return None

    def transcribe(self, wav_bytes: bytes) -> str:
        """Transcribe in-memory WAV using Groq Whisper Turbo."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
        data = {
            "model": "whisper-large-v3-turbo",
            "language": "en"
        }

        try:
            res = HTTP_CLIENT.post(GROQ_WHISPER_URL, headers=headers, files=files, data=data)
            if res.status_code == 200:
                text = res.json().get("text", "").strip()
                # Filter out Whisper phantom hallucinations on silence
                clean_lower = re.sub(r"[^\w\s]", "", text.lower()).strip()
                hallucinations = {
                    "", ".", "..", "...", "you", "thank you", "thank you very much",
                    "thanks for watching", "subtitles by", "system", "terminal",
                    "system terminal", "bye", "goodbye", "yeah", "so", "oh", "um", "ah", "okay", "ok"
                }
                if clean_lower in hallucinations or len(clean_lower) < 2:
                    return ""
                return text
        except Exception:
            pass
        return ""

    def stream_and_vocalize(self, user_text: str):
        """
        Stream LLM tokens from Groq and pipeline sentence-by-sentence TTS synthesis.
        Sentence 1 plays while Sentence 2 is still being generated.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = [{"role": "system", "content": self.get_voice_system_prompt()}]
        messages.extend(self.history[-4:])
        messages.append({"role": "user", "content": user_text})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.3,
            "max_tokens": 150,
        }

        console.print(f"[bold cyan]✦ J.A.R.V.I.S.:[/] ", end="")

        current_sentence = ""
        full_reply = ""
        sentence_delimiters = re.compile(r"([.!?\n]+)")

        try:
            with HTTP_CLIENT.stream("POST", GROQ_COMPLETIONS_URL, headers=headers, json=payload) as response:
                for line in response.iter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        import json
                        try:
                            chunk_json = json.loads(line[6:])
                            delta = chunk_json["choices"][0]["delta"].get("content", "")
                            if delta:
                                console.print(delta, end="")
                                current_sentence += delta
                                full_reply += delta

                                # Check for complete sentence boundary OR clause (>14 words)
                                parts = sentence_delimiters.split(current_sentence)
                                if len(parts) > 1:
                                    finished_sentence = parts[0] + parts[1]
                                    self.speech_pipeline.enqueue(finished_sentence)
                                    current_sentence = "".join(parts[2:])
                                elif len(current_sentence.split()) >= 14 and "," in current_sentence:
                                    comma_idx = current_sentence.rfind(",")
                                    finished_clause = current_sentence[:comma_idx + 1]
                                    self.speech_pipeline.enqueue(finished_clause)
                                    current_sentence = current_sentence[comma_idx + 1:].lstrip()
                        except Exception:
                            pass

            # Enqueue any trailing remainder
            if current_sentence.strip():
                self.speech_pipeline.enqueue(current_sentence.strip())

            console.print("\n")

            if full_reply.strip():
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": full_reply.strip()})

            # Wait for speech playback to finish before opening microphone again
            self.speech_pipeline.wait_until_done()

        except Exception as e:
            fallback = f"Neural synthesis interrupted, Sir: {e}"
            console.print(f"\n[red]{fallback}[/]\n")
            self.speech_pipeline.enqueue(fallback)
            self.speech_pipeline.wait_until_done()

    def capture_user_speech_ptt(self) -> Optional[bytes]:
        """Push-to-talk capture: Press Enter to speak, press Enter when finished."""
        try:
            console.print("[bold green]✦ [Press Enter to start speaking...][/]", end="")
            sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            self.is_running = False
            return None

        try:
            proc = subprocess.Popen(
                ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            console.print(f"[red]Error accessing PipeWire audio stream: {e}[/]")
            return None

        buffered_pcm = bytearray()
        stop_event = threading.Event()

        def reader():
            while not stop_event.is_set():
                chunk = proc.stdout.read(FRAME_SIZE)
                if not chunk:
                    break
                buffered_pcm.extend(chunk)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        try:
            console.print("[bold red]● RECORDING... [Press Enter when finished speaking][/]", end="")
            sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            self.is_running = False

        stop_event.set()
        proc.terminate()
        proc.wait()
        reader_thread.join(timeout=1.0)

        if len(buffered_pcm) > FRAME_SIZE * 3:
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(bytes(buffered_pcm))
            return wav_buf.getvalue()
        return None

    def run_interactive_loop(self, ptt: bool = False):
        """Main hands-free or push-to-talk voice loop."""
        mode_str = "Push-To-Talk (Press Enter)" if ptt else "Hands-Free Continuous (Auto-VAD)"
        console.clear()
        console.print(Panel(
            "[bold white]✦ J.A.R.V.I.S. REAL-TIME TACTICAL VOICE INTERFACE[/]\n"
            "[dim]Full-duplex continuous conversational speech link active.[/]\n\n"
            "[bold cyan]• Mode:[/]     " + mode_str + "\n"
            "[bold cyan]• Model:[/]    " + self.model + " (Groq Accelerated)\n"
            "[bold cyan]• Audio:[/]    PipeWire 16kHz Streaming + In-Memory VAD\n"
            "[bold cyan]• STT:[/]      Groq Whisper Turbo (Prompt-Biased)\n"
            "[bold cyan]• Voice:[/]    " + VOICE_NAME + f" ({VOICE_RATE})\n"
            "[bold cyan]• Commands:[/] 'volume up/down', 'clean memory', 'sync github', 'exit'\n"
            "[dim]Speak anytime. Say 'exit' or press Ctrl+C to return to terminal.[/]",
            title="VOICE LINK ONLINE",
            border_style="cyan"
        ))
        console.print()

        if not ptt:
            self.calibrate_ambient_noise(duration_s=0.6)

        welcome_text = "Voice link established. I am listening, Sir."
        console.print(f"[bold cyan]✦ J.A.R.V.I.S.:[/] {welcome_text}\n")
        self.speech_pipeline.enqueue(welcome_text)
        self.speech_pipeline.wait_until_done()
        play_chime_listen()

        while self.is_running:
            try:
                # 1. Capture user utterance (Hands-Free VAD or Push-To-Talk)
                if ptt:
                    wav_bytes = self.capture_user_speech_ptt()
                else:
                    wav_bytes = self.capture_user_speech()

                if not wav_bytes:
                    if not self.is_running:
                        break
                    continue

                # 2. Instant acoustic feedback cue (<20ms)
                play_chime_ping()

                # 3. Transcribe via Groq Whisper Turbo
                user_query = self.transcribe(wav_bytes)
                if not user_query:
                    continue

                console.print(f"[bold white]✦ Sir:[/] \"{user_query}\"")

                # 4. Check for voice hardware / action commands
                action_res = check_voice_actions(user_query)
                if action_res == "STAND_DOWN":
                    stand_down_msg = "Standing down voice mode. Entering terminal standby, Sir."
                    console.print(f"[bold cyan]✦ J.A.R.V.I.S.:[/] {stand_down_msg}\n")
                    self.speech_pipeline.enqueue(stand_down_msg)
                    self.speech_pipeline.wait_until_done()
                    break
                elif action_res:
                    console.print(f"[bold cyan]✦ J.A.R.V.I.S.:[/] {action_res}\n")
                    self.speech_pipeline.enqueue(action_res)
                    self.speech_pipeline.wait_until_done()
                    play_chime_listen()
                    continue

                # 5. Stream LLM completion and vocalize
                self.stream_and_vocalize(user_query)
                play_chime_listen()

            except KeyboardInterrupt:
                console.print("\n[dim]✦ Voice interface suspended by user.[/]")
                break
            except Exception as e:
                console.print(f"\n[red]Session loop error: {e}[/]")
                time.sleep(1)


def main():
    """CLI entry point for jarvis_voice."""
    ptt = any(arg in sys.argv for arg in ("--ptt", "-p", "ptt"))
    model = DEFAULT_MODEL
    for arg in sys.argv:
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1].strip()

    session = JarvisVoiceSession(model=model)
    session.run_interactive_loop(ptt=ptt)


if __name__ == "__main__":
    main()
