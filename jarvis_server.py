#!/usr/bin/env python3
"""
J.A.R.V.I.S. Local Network Hub & Multi-Device Web Server
Exposes a responsive mobile/desktop tactical HUD on 0.0.0.0:8765
Supports "Hey Jarvis" continuous voice recognition, TTS audio streaming,
remote laptop control, telemetry dashboard, and QR code pairing.
"""

import os
import sys
import time
import json
import socket
import asyncio
import subprocess
import threading
import re
from pathlib import Path
from typing import Any, Optional

from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import httpx
import edge_tts
import qrcode
import io
import urllib.parse

app = Flask(__name__)
CORS(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
if not GROQ_API_KEY:
    try:
        with open(Path.home() / ".local/bin/jarvis") as f:
            for line in f:
                if "export GROQ_API_KEY=" in line:
                    GROQ_API_KEY = line.split("=", 1)[1].strip().strip("\"'")
                    break
    except Exception:
        pass
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
OFFLINE_URL = "http://localhost:11434/api/chat"
OFFLINE_MODEL = "qwen2.5-coder:1.5b"
VOICE_NAME = "en-GB-RyanNeural"
PORT = 8765

# Persistent HTTP Client with Connection Pooling for Ultra-Low Latency
HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(8.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
)


def get_lan_ip() -> str:
    """Detect local LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not actually connect or send packets
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_telemetry() -> dict[str, Any]:
    """Capture live hardware and system metrics."""
    telemetry = {
        "time": time.strftime("%H:%M:%S"),
        "cpu": "0%",
        "ram": "0%",
        "swap": "0%",
        "uptime": "unknown",
        "battery": "AC",
        "lan_ip": get_lan_ip(),
        "hostname": socket.gethostname(),
        "port": PORT,
    }
    # CPU
    try:
        with open("/proc/loadavg", "r") as f:
            telemetry["cpu"] = f.read().split()[0]
    except Exception:
        pass

    # RAM
    try:
        res = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=1)
        for line in res.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                used, total = int(parts[2]), int(parts[1])
                telemetry["ram"] = f"{used}M / {total}M ({int(used/total*100)}%)"
            elif line.startswith("Swap:"):
                parts = line.split()
                s_used, s_total = int(parts[2]), int(parts[1])
                if s_total > 0:
                    telemetry["swap"] = f"{s_used}M / {s_total}M"
    except Exception:
        pass

    # Uptime
    try:
        with open("/proc/uptime", "r") as f:
            sec = float(f.read().split()[0])
            hours = int(sec // 3600)
            mins = int((sec % 3600) // 60)
            telemetry["uptime"] = f"{hours}h {mins}m"
    except Exception:
        pass

    # Battery
    bat_path = Path("/sys/class/power_supply/BAT0/capacity")
    if bat_path.exists():
        try:
            telemetry["battery"] = f"{bat_path.read_text().strip()}%"
        except Exception:
            pass

    return telemetry


# ── System Control Actions ───────────────────────────────────────────────────
def execute_device_action(action: str, params: Optional[dict] = None) -> dict[str, Any]:
    """Execute pre-authorized hardware and system commands."""
    params = params or {}
    action = action.lower().strip()

    if action == "volume_up":
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"], capture_output=True)
        return {"status": "ok", "message": "Volume increased by 10%"}

    elif action == "volume_down":
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"], capture_output=True)
        return {"status": "ok", "message": "Volume decreased by 10%"}

    elif action == "volume_mute":
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], capture_output=True)
        return {"status": "ok", "message": "Volume mute toggled"}

    elif action == "lock_screen":
        subprocess.run(["loginctl", "lock-session"], capture_output=True)
        return {"status": "ok", "message": "Screen locked successfully"}

    elif action == "clean_memory":
        script = Path.home() / ".local/bin/turbo-clean"
        if script.exists():
            proc = subprocess.run([str(script)], capture_output=True, text=True, timeout=15)
            return {"status": "ok", "message": proc.stdout.strip() or "Memory purged"}
        return {"status": "error", "message": "turbo-clean not found"}

    elif action == "daily_sync":
        script = Path.home() / ".local/bin/antigravity-daily-sync"
        if script.exists():
            proc = subprocess.run([str(script)], capture_output=True, text=True, timeout=30)
            return {"status": "ok", "message": proc.stdout.strip() or "Daily sync executed"}
        return {"status": "error", "message": "antigravity-daily-sync not found"}

    elif action == "notify":
        title = params.get("title", "J.A.R.V.I.S. Alert")
        body = params.get("body", "Message from remote device")
        subprocess.run(["notify-send", "-i", "dialog-information", title, body], capture_output=True)
        return {"status": "ok", "message": "Notification posted"}

    elif action == "bash":
        cmd = params.get("command", "").strip()
        if not cmd:
            return {"status": "error", "message": "Empty command"}
        # Security: block destructive commands
        if re.search(r"\b(rm\s+-rf\s+/|mkfs|dd\s+if=)\b", cmd):
            return {"status": "error", "message": "Command blocked by security policy"}
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
        return {
            "status": "ok",
            "output": (proc.stdout + proc.stderr)[:3000],
            "returncode": proc.returncode,
        }

    return {"status": "error", "message": f"Unknown action: {action}"}


# ── AI Completion & Autonomous Execution ─────────────────────────────────────
SYSTEM_PROMPT = """You are J.A.R.V.I.S., an advanced AI terminal and smart hub assistant engineered for Linux pair-programming, system diagnostics, and remote orchestration.
Persona:
- Address the user as "Sir".
- Deliver crisp, precise, and technically flawless responses.
- You can execute terminal commands or control the laptop directly.
- If the user asks you to perform an action (e.g. adjust volume, purge memory, lock screen, sync github, check system), perform it and confirm concisely.
Keep spoken summaries under 35 words so voice delivery remains rapid and natural."""


def ask_ai(prompt: str) -> tuple[str, str]:
    """Query Groq or offline Ollama, return (full_text, spoken_summary)."""
    tele = get_telemetry()
    tele_context = (
        f"\n[CURRENT LIVE SYSTEM TELEMETRY ON LAPTOP]:\n"
        f"Host: {tele.get('hostname')} | LAN IP: {tele.get('lan_ip')} | Time: {tele.get('time')}\n"
        f"CPU Load: {tele.get('cpu')} | RAM: {tele.get('ram')} | Swap: {tele.get('swap')}\n"
        f"Battery: {tele.get('battery')} | Uptime: {tele.get('uptime')}\n"
    )
    dynamic_system = SYSTEM_PROMPT + tele_context

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": dynamic_system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    answer = ""
    try:
        resp = HTTP_CLIENT.post(GROQ_URL, headers=headers, json=payload)
        if resp.status_code == 200:
            answer = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        pass

    # Fallback to local Ollama if Groq fails or offline
    if not answer:
        try:
            resp = HTTP_CLIENT.post(
                OFFLINE_URL,
                json={
                    "model": OFFLINE_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                },
                timeout=10.0
            )
            if resp.status_code == 200:
                answer = resp.json()["message"]["content"]
        except Exception as e:
            answer = f"I am unable to reach the inference servers, Sir. Error: {e}"

    # Extract concise spoken summary
    lines = [l.strip() for l in answer.splitlines() if l.strip() and not l.strip().startswith("```")]
    spoken = " ".join(lines[:2])
    spoken = re.sub(r"[*_#`]", "", spoken)
    if len(spoken.split()) > 35:
        spoken = " ".join(spoken.split()[:35]) + "..."
    if not spoken:
        spoken = "Standing by, Sir."

    return answer, spoken


# ── Web Application UI (HTML/CSS/JS) ─────────────────────────────────────────
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="theme-color" content="#000000">
  <title>✦ J.A.R.V.I.S. TACTICAL HUB</title>
  <style>
    :root {
      --bg: #000000;
      --card-bg: #0a0a0a;
      --border: #222222;
      --fg: #ffffff;
      --dim: #777777;
      --accent: #00ffcc;
      --accent-dim: rgba(0, 255, 204, 0.15);
      --pulse-glow: rgba(0, 255, 204, 0.4);
      --font: 'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body {
      background: var(--bg);
      color: var(--fg);
      font-family: var(--font);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }
    header {
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #050505;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand-icon {
      font-size: 1.2rem;
      color: var(--accent);
      animation: pulse 2.5s infinite ease-in-out;
    }
    .brand-title {
      font-size: 0.95rem;
      font-weight: 700;
      letter-spacing: 1.5px;
    }
    .badge {
      font-size: 0.65rem;
      padding: 3px 7px;
      border-radius: 4px;
      border: 1px solid var(--accent);
      color: var(--accent);
      background: var(--accent-dim);
      letter-spacing: 0.5px;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 0.8; }
      50% { transform: scale(1.15); opacity: 1; filter: drop-shadow(0 0 8px var(--accent)); }
    }

    /* Telemetry Grid */
    .telemetry {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      padding: 12px 16px;
      background: #040404;
      border-bottom: 1px solid var(--border);
    }
    .tele-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      padding: 8px 10px;
      border-radius: 6px;
      text-align: center;
    }
    .tele-label {
      font-size: 0.6rem;
      color: var(--dim);
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 4px;
    }
    .tele-val {
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--fg);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Voice & Wake-word Indicator */
    .wake-status {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 10px 16px;
      font-size: 0.75rem;
      color: var(--dim);
      background: #020202;
      border-bottom: 1px dashed var(--border);
    }
    .wake-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #444;
    }
    .wake-dot.listening {
      background: var(--accent);
      box-shadow: 0 0 10px var(--accent);
      animation: blink 1s infinite alternate;
    }
    @keyframes blink {
      from { opacity: 0.3; } to { opacity: 1; }
    }

    /* Waveform Canvas */
    canvas#visualizer {
      width: 100%;
      height: 48px;
      background: #000;
      display: block;
      border-bottom: 1px solid var(--border);
    }

    /* Main Chat Stream */
    .chat-container {
      flex: 1;
      padding: 16px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .message {
      max-width: 88%;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.85rem;
      line-height: 1.45;
      word-break: break-word;
    }
    .msg-user {
      align-self: flex-end;
      background: #181818;
      border: 1px solid #333;
      color: #fff;
    }
    .msg-jarvis {
      align-self: flex-start;
      background: #0b0f0e;
      border: 1px solid #1a332d;
      color: #e0fbf4;
    }
    .msg-jarvis .sender {
      font-size: 0.65rem;
      color: var(--accent);
      margin-bottom: 4px;
      font-weight: 700;
      letter-spacing: 1px;
    }

    /* Quick Action Buttons */
    .actions-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
      padding: 8px 16px;
      background: #030303;
      border-top: 1px solid var(--border);
    }
    .btn-action {
      background: #111;
      border: 1px solid var(--border);
      color: #ccc;
      padding: 9px 6px;
      border-radius: 6px;
      font-family: inherit;
      font-size: 0.72rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      transition: all 0.15s ease;
    }
    .btn-action:hover, .btn-action:active {
      background: #222;
      border-color: var(--accent);
      color: #fff;
    }

    /* Voice & Input Controls */
    .input-bar {
      display: flex;
      gap: 8px;
      padding: 12px 16px 16px 16px;
      background: #050505;
      border-top: 1px solid var(--border);
      align-items: center;
    }
    .input-bar input {
      flex: 1;
      background: #111;
      border: 1px solid var(--border);
      color: #fff;
      font-family: inherit;
      font-size: 0.88rem;
      padding: 12px 14px;
      border-radius: 6px;
      outline: none;
    }
    .input-bar input:focus {
      border-color: var(--accent);
    }
    .btn-icon {
      width: 44px;
      height: 44px;
      background: #111;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 1.1rem;
      flex-shrink: 0;
      transition: all 0.15s ease;
    }
    .btn-mic.active {
      background: #2a0812;
      border-color: #ff3366;
      color: #ff3366;
      box-shadow: 0 0 16px rgba(255, 51, 102, 0.7);
      animation: micGlow 0.9s infinite alternate;
    }
    @keyframes micGlow {
      from { transform: scale(1); box-shadow: 0 0 8px rgba(255, 51, 102, 0.4); }
      to { transform: scale(1.08); box-shadow: 0 0 18px rgba(255, 51, 102, 0.9); }
    }
    .btn-send {
      background: #fff;
      color: #000;
      font-weight: 700;
      border: none;
    }

    @media (max-width: 600px) {
      .telemetry { grid-template-columns: repeat(2, 1fr); }
      .actions-grid { grid-template-columns: repeat(3, 1fr); }
    }
  </style>
<body>

  <!-- Mobile Secure Context Banner -->
  <div id="https-banner" style="display:none; background: #1a1200; border-bottom: 1px solid #d97706; color: #fbbf24; font-size: 0.76rem; padding: 10px 14px; text-align: center;">
    🔒 Phone browsers require HTTPS for inline microphone. <a id="https-link" href="#" style="color: #00ffcc; font-weight: bold; text-decoration: underline; margin-left: 6px;">Tap to open HTTPS (port 8766)</a>
  </div>

  <header>
    <div class="brand">
      <span class="brand-icon">✦</span>
      <span class="brand-title">J.A.R.V.I.S. HUB</span>
    </div>
    <span class="badge" id="conn-badge">ONLINE</span>
  </header>

  <!-- Live Telemetry -->
  <div class="telemetry">
    <div class="tele-card">
      <div class="tele-label">CPU LOAD</div>
      <div class="tele-val" id="t-cpu">--</div>
    </div>
    <div class="tele-card">
      <div class="tele-label">RAM USAGE</div>
      <div class="tele-val" id="t-ram">--</div>
    </div>
    <div class="tele-card">
      <div class="tele-label">BATTERY</div>
      <div class="tele-val" id="t-bat">--</div>
    </div>
    <div class="tele-card">
      <div class="tele-label">UPTIME</div>
      <div class="tele-val" id="t-up">--</div>
    </div>
  </div>

  <!-- Wake Word Status -->
  <div class="wake-status">
    <div class="wake-dot" id="wake-dot"></div>
    <span id="wake-text">Continuous Voice: Say "Hey Jarvis" or tap mic</span>
  </div>

  <!-- Waveform Visualizer -->
  <canvas id="visualizer"></canvas>

  <!-- Chat Log -->
  <div class="chat-container" id="chat">
    <div class="message msg-jarvis">
      <div class="sender">✦ J.A.R.V.I.S.</div>
      Systems operational, Sir. Connected to laptop across local network. Say "Hey Jarvis" or send a command.
    </div>
  </div>

  <!-- Action Grid -->
  <div class="actions-grid">
    <button class="btn-action" onclick="sendAction('volume_up')">🔊 Vol +</button>
    <button class="btn-action" onclick="sendAction('volume_down')">🔉 Vol -</button>
    <button class="btn-action" onclick="sendAction('volume_mute')">🔇 Mute</button>
    <button class="btn-action" onclick="sendAction('clean_memory')">⚡ Clean RAM</button>
    <button class="btn-action" onclick="sendAction('daily_sync')">🔄 Git Sync</button>
    <button class="btn-action" onclick="sendAction('lock_screen')">🔒 Lock PC</button>
  </div>

  <!-- Input Bar -->
  <div class="input-bar">
    <button class="btn-icon btn-mic" id="mic-btn" onclick="toggleVoiceRecording()" title="Tap to Speak with J.A.R.V.I.S.">🎙️</button>
    <input type="text" id="query-input" placeholder="Type or tap mic to speak..." autocomplete="off" onkeydown="if(event.key==='Enter') submitText()">
    <button class="btn-icon btn-send" onclick="submitText()">➤</button>
  </div>

  <audio id="tts-player" style="display:none;"></audio>
  <input type="file" id="native-audio-input" accept="audio/*" capture="microphone" style="display:none;">

  <script>
    const chatBox = document.getElementById('chat');
    const queryInput = document.getElementById('query-input');
    const micBtn = document.getElementById('mic-btn');
    const wakeDot = document.getElementById('wake-dot');
    const wakeText = document.getElementById('wake-text');
    const ttsPlayer = document.getElementById('tts-player');

    // ── Telemetry Poller ──────────────────────────────────────────────────
    async function updateTelemetry() {
      try {
        const res = await fetch('/api/telemetry');
        if (res.ok) {
          const data = await res.json();
          document.getElementById('t-cpu').textContent = data.cpu || '0%';
          document.getElementById('t-ram').textContent = (data.ram || '').split(' ')[0] || '--';
          document.getElementById('t-bat').textContent = data.battery || 'AC';
          document.getElementById('t-up').textContent = data.uptime || '--';
          document.getElementById('conn-badge').textContent = 'ONLINE';
          document.getElementById('conn-badge').style.borderColor = '#00ffcc';
        }
      } catch (e) {
        document.getElementById('conn-badge').textContent = 'OFFLINE';
        document.getElementById('conn-badge').style.borderColor = '#ff3366';
      }
    }
    setInterval(updateTelemetry, 3000);
    updateTelemetry();

    // ── Chat & Action Dispatcher ──────────────────────────────────────────
    function appendMessage(sender, text, isJarvis = false) {
      const msg = document.createElement('div');
      msg.className = `message ${isJarvis ? 'msg-jarvis' : 'msg-user'}`;
      if (isJarvis) {
        msg.innerHTML = `<div class="sender">✦ J.A.R.V.I.S.</div>` + text.replace(/\\n/g, '<br>');
      } else {
        msg.textContent = text;
      }
      chatBox.appendChild(msg);
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    async function sendPrompt(promptText) {
      if (!promptText.trim()) return;
      appendMessage('User', promptText, false);
      queryInput.value = '';

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: promptText })
        });
        const data = await res.json();
        appendMessage('J.A.R.V.I.S.', data.text || 'Command executed.', true);

        // Play voice audio
        if (data.audio_url) {
          ttsPlayer.src = data.audio_url + '?t=' + Date.now();
          ttsPlayer.play().catch(e => console.log('Audio playback prevented:', e));
          animateVisualizer();
        }
      } catch (e) {
        appendMessage('J.A.R.V.I.S.', 'Connection error with host, Sir: ' + e, true);
      }
    }

    async function sendAction(actionName) {
      try {
        const res = await fetch('/api/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: actionName })
        });
        const data = await res.json();
        appendMessage('J.A.R.V.I.S.', `[${actionName}] ${data.message || 'Executed'}`, true);
      } catch (e) {
        appendMessage('J.A.R.V.I.S.', `Failed to execute ${actionName}: ${e}`, true);
      }
    }

    function submitText() {
      const val = queryInput.value.trim();
      if (val) sendPrompt(val);
    }

    // ── Direct Microphone Audio Recording & Speech Engine ──────────────
    let mediaRecorder = null;
    let audioChunks = [];
    let micStream = null;
    let audioContext = null;
    let analyserNode = null;
    let isRecording = false;
    let silenceTimer = null;
    let speechActive = false;

    async function toggleVoiceRecording() {
      if (isRecording) {
        stopVoiceRecording();
      } else {
        startVoiceRecording();
      }
    }

    async function startVoiceRecording() {
      // Pre-unlock audio element for mobile browser autoplay
      try {
        ttsPlayer.play().then(() => ttsPlayer.pause()).catch(() => {});
      } catch (e) {}

      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        const nativeInput = document.getElementById('native-audio-input');
        if (nativeInput) {
          wakeText.textContent = '🎙️ Opening mobile voice recorder...';
          nativeInput.click();
          return;
        }
        alert('Microphone access is restricted on insecure HTTP. Please tap the banner above to switch to HTTPS on port 8766.');
        return;
      }

      try {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          }
        });
      } catch (err) {
        console.error('Microphone error:', err);
        wakeText.textContent = 'Mic permission denied. Please allow microphone.';
        return;
      }

      // Real-time AudioContext Analyser for Live Canvas Waveform
      try {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioContext.createMediaStreamSource(micStream);
        analyserNode = audioContext.createAnalyser();
        analyserNode.fftSize = 64;
        source.connect(analyserNode);
        visualizeLiveMic();
      } catch (e) {
        console.warn('AudioContext setup error:', e);
      }

      let mimeType = 'audio/webm;codecs=opus';
      if (!MediaRecorder.isTypeSupported(mimeType)) {
        mimeType = MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : '';
      }
      const recOptions = mimeType ? { mimeType } : {};

      audioChunks = [];
      mediaRecorder = new MediaRecorder(micStream, recOptions);
      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) audioChunks.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        const audioBlob = new Blob(audioChunks, { type: mimeType || 'audio/webm' });
        await uploadAndProcessVoice(audioBlob);
      };

      mediaRecorder.start(80);
      isRecording = true;
      speechActive = false;
      micBtn.classList.add('active');
      wakeDot.classList.add('listening');
      wakeText.textContent = '🎙️ Listening... Speak now (Tap mic when done)';

      runClientVAD();
    }

    function runClientVAD() {
      if (!analyserNode || !isRecording) return;
      const dataArr = new Uint8Array(analyserNode.frequencyBinCount);

      function check() {
        if (!isRecording) return;
        analyserNode.getByteFrequencyData(dataArr);
        let sum = 0;
        for (let i = 0; i < dataArr.length; i++) sum += dataArr[i];
        const avg = sum / dataArr.length;

        if (avg > 16) {
          speechActive = true;
          if (silenceTimer) {
            clearTimeout(silenceTimer);
            silenceTimer = null;
          }
        } else if (speechActive) {
          if (!silenceTimer) {
            silenceTimer = setTimeout(() => {
              if (isRecording && speechActive) {
                stopVoiceRecording();
              }
            }, 1400);
          }
        }
        requestAnimationFrame(check);
      }
      requestAnimationFrame(check);
    }

    function stopVoiceRecording() {
      if (!isRecording) return;
      isRecording = false;
      if (silenceTimer) {
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
      micBtn.classList.remove('active');
      wakeDot.classList.remove('listening');
      wakeText.textContent = '⚡ Transcribing & executing with J.A.R.V.I.S....';

      if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
      }
      if (micStream) {
        micStream.getTracks().forEach(t => t.stop());
      }
    }

    async function uploadAndProcessVoice(blob) {
      if (!blob || blob.size < 400) {
        wakeText.textContent = 'Recording too brief. Tap mic to talk.';
        return;
      }

      const fd = new FormData();
      fd.append('file', blob, 'speech.webm');

      try {
        const res = await fetch('/api/voice', { method: 'POST', body: fd });
        const data = await res.json();

        if (data.transcript) {
          appendMessage('User', '🎤 ' + data.transcript, false);
        }
        if (data.text) {
          appendMessage('J.A.R.V.I.S.', data.text, true);
        }

        if (data.audio_url) {
          wakeText.textContent = '🔊 J.A.R.V.I.S. is speaking...';
          ttsPlayer.src = data.audio_url + '?t=' + Date.now();
          ttsPlayer.play().catch(e => console.log('Audio playback notice:', e));
          animateVisualizer();
          ttsPlayer.onended = () => {
            wakeText.textContent = '✦ Voice Ready: Tap mic to speak.';
          };
        } else {
          wakeText.textContent = '✦ Voice Ready: Tap mic to speak.';
        }
      } catch (err) {
        appendMessage('J.A.R.V.I.S.', 'Voice processing error: ' + err, true);
        wakeText.textContent = 'Error processing voice. Tap mic to retry.';
      }
    }

    // ── Native Audio Input Fallback (for Insecure Context HTTP on phones) ──
    const nativeInput = document.getElementById('native-audio-input');
    if (nativeInput) {
      nativeInput.addEventListener('change', async (e) => {
        const file = e.target.files && e.target.files[0];
        if (file) {
          wakeText.textContent = '⚡ Transcribing & executing with J.A.R.V.I.S....';
          await uploadAndProcessVoice(file);
          nativeInput.value = '';
        }
      });
    }

    // ── Mobile Insecure Context Detection ──────────────────────────────
    if (!window.isSecureContext && window.location.protocol === 'http:' && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
      const banner = document.getElementById('https-banner');
      const link = document.getElementById('https-link');
      if (banner && link) {
        link.href = `https://${window.location.hostname}:8766`;
        banner.style.display = 'block';
      }
    }

    // ── Visualizer Canvas ──────────────────────────────────────────────
    const canvas = document.getElementById('visualizer');
    const ctx = canvas.getContext('2d');
    let animId = null;

    function resizeCanvas() {
      canvas.width = canvas.clientWidth * window.devicePixelRatio;
      canvas.height = canvas.clientHeight * window.devicePixelRatio;
    }
    window.addEventListener('resize', resizeCanvas);
    resizeCanvas();

    function visualizeLiveMic() {
      if (!analyserNode) return;
      const bufferLen = analyserNode.frequencyBinCount;
      const dataArr = new Uint8Array(bufferLen);

      function drawMic() {
        if (!isRecording) return;
        analyserNode.getByteTimeDomainData(dataArr);

        ctx.fillStyle = '#000000';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        ctx.lineWidth = 2.5;
        ctx.strokeStyle = '#ff3366';
        ctx.beginPath();

        const sliceWidth = canvas.width / bufferLen;
        let x = 0;
        for (let i = 0; i < bufferLen; i++) {
          const v = dataArr[i] / 128.0;
          const y = (v * canvas.height) / 2;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
          x += sliceWidth;
        }
        ctx.stroke();
        requestAnimationFrame(drawMic);
      }
      drawMic();
    }

    function animateVisualizer() {
      let step = 0;
      cancelAnimationFrame(animId);
      function draw() {
        ctx.fillStyle = '#000000';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        ctx.lineWidth = 2;
        ctx.strokeStyle = '#00ffcc';
        ctx.beginPath();

        const sliceWidth = canvas.width / 40;
        let x = 0;

        for (let i = 0; i < 40; i++) {
          const v = Math.sin(step * 0.15 + i * 0.3) * (canvas.height * 0.35);
          const y = (canvas.height / 2) + v;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
          x += sliceWidth;
        }
        ctx.stroke();
        step++;
        if (!ttsPlayer.paused) {
          animId = requestAnimationFrame(draw);
        } else {
          ctx.fillStyle = '#000000';
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.strokeStyle = '#222222';
          ctx.beginPath();
          ctx.moveTo(0, canvas.height / 2);
          ctx.lineTo(canvas.width, canvas.height / 2);
          ctx.stroke();
        }
      }
      draw();
    }
  </script>
</body>
</html>
"""


# ── Server Routes ────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    """Serve the responsive tactical HUD application."""
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/telemetry", methods=["GET"])
def api_telemetry():
    """Return live machine telemetry."""
    return jsonify(get_telemetry())


@app.route("/api/action", methods=["POST"])
def api_action():
    """Execute pre-authorized hardware and device controls."""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")
    params = data.get("params", {})
    res = execute_device_action(action, params)
    return jsonify(res)


def process_prompt_logic(prompt: str) -> dict[str, Any]:
    """Execute autonomous actions, free APIs, or Groq LLM inference, return dict with text, spoken, audio_url."""
    prompt_lower = prompt.lower().strip()

    # Instant autonomous action spotting & direct responses (< 1ms)
    if "volume up" in prompt_lower or "louder" in prompt_lower:
        execute_device_action("volume_up")
        spoken = "Volume increased by 10%, Sir."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "volume down" in prompt_lower or "quieter" in prompt_lower:
        execute_device_action("volume_down")
        spoken = "Volume decreased by 10%, Sir."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "mute" in prompt_lower:
        execute_device_action("volume_mute")
        spoken = "Mute toggled, Sir."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "clean" in prompt_lower and ("memory" in prompt_lower or "ram" in prompt_lower or "cache" in prompt_lower):
        res = execute_device_action("clean_memory")
        spoken = f"{res.get('message', 'Memory purged')}, Sir."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "lock" in prompt_lower and ("screen" in prompt_lower or "pc" in prompt_lower or "laptop" in prompt_lower):
        execute_device_action("lock_screen")
        spoken = "Screen locked, Sir."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif any(q in prompt_lower for q in ("what's the time", "what is the time", "what time", "tell me the time", "current time", "the time", "time now")) or prompt_lower == "time":
        spoken = f"Sir, the current time is {time.strftime('%I:%M %p')}."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "battery" in prompt_lower:
        tele = get_telemetry()
        spoken = f"Sir, the battery is at {tele.get('battery', 'unknown')}."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    elif "uptime" in prompt_lower or ("how long" in prompt_lower and ("pc" in prompt_lower or "system" in prompt_lower or "on" in prompt_lower)):
        tele = get_telemetry()
        spoken = f"Sir, system uptime is {tele.get('uptime', 'unknown')}."
        return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}

    # Free Public APIs Direct Integration
    sys.path.insert(0, "/home/feds/.gemini/antigravity/scratch/free-apis")
    try:
        from free_apis import query_live_api
        if "weather" in prompt_lower:
            loc = prompt_lower.replace("weather", "").replace("what's the", "").replace("what is the", "").replace("in", "").strip()
            res = query_live_api("weather", loc)
            spoken = f"Weather report: {res}, Sir."
            return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
        elif any(k in prompt_lower for k in ("bitcoin", "crypto", "btc", "eth")):
            coin = "ethereum" if "eth" in prompt_lower else "bitcoin"
            res = query_live_api("crypto", coin)
            spoken = f"{res}, Sir."
            return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
        elif "joke" in prompt_lower:
            res = query_live_api("joke")
            spoken = f"Here is one for you, Sir: {res}"
            return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
        elif "quote" in prompt_lower or "inspiration" in prompt_lower:
            res = query_live_api("quote")
            spoken = f"{res}, Sir."
            return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
        elif "my ip" in prompt_lower or "public ip" in prompt_lower:
            res = query_live_api("ip")
            spoken = f"{res}, Sir."
            return {"text": spoken, "spoken": spoken, "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"}
    except Exception:
        pass

    full_answer, spoken = ask_ai(prompt)
    audio_url = f"/api/tts?text={urllib.parse.quote(spoken)}"
    return {
        "text": full_answer,
        "spoken": spoken,
        "audio_url": audio_url,
    }


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Process text or voice prompt, trigger autonomous actions, return response + audio."""
    data = request.get_json(force=True, silent=True) or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"text": "Awaiting your command, Sir.", "audio_url": None})
    return jsonify(process_prompt_logic(prompt))


@app.route("/api/voice", methods=["POST"])
def api_voice():
    """Receive recorded voice audio blob from web browser, transcribe, execute, return speech."""
    if "file" not in request.files:
        return jsonify({"error": "No audio file received"}), 400

    audio_file = request.files["file"]
    content = audio_file.read()
    if len(content) < 400:
        return jsonify({
            "transcript": "",
            "text": "Audio was too short, Sir. Please tap the mic and speak again.",
            "spoken": "Audio too short, Sir.",
            "audio_url": None
        })

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (audio_file.filename or "recording.webm", content, audio_file.content_type or "audio/webm")}
    data = {"model": "whisper-large-v3-turbo", "language": "en"}

    transcript = ""
    try:
        resp = HTTP_CLIENT.post(GROQ_WHISPER_URL, headers=headers, files=files, data=data)
        if resp.status_code == 200:
            transcript = resp.json().get("text", "").strip()
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {e}"}), 500

    clean_check = re.sub(r"[^\w\s]", "", transcript.lower()).strip()
    hallucinations = {"", ".", "..", "...", "you", "thank you", "thanks for watching", "subtitles by", "bye"}
    if clean_check in hallucinations or len(clean_check) < 2:
        return jsonify({
            "transcript": "",
            "text": "I did not detect any spoken words, Sir. Please try again.",
            "spoken": "I did not detect any speech, Sir.",
            "audio_url": None
        })

    # Strip leading "Hey Jarvis" or "Jarvis"
    wake_match = re.search(r"^\s*(hey\s+jarvis|jarvis)[,:]?\s*", transcript, re.IGNORECASE)
    prompt = transcript[wake_match.end():].strip() if wake_match else transcript

    if not prompt:
        spoken = "Yes, Sir? Standing by for your instructions."
        return jsonify({
            "transcript": transcript,
            "text": spoken,
            "spoken": spoken,
            "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
        })

    result = process_prompt_logic(prompt)
    result["transcript"] = transcript
    return jsonify(result)


@app.route("/api/audio/<filename>", methods=["GET"])
def api_audio(filename):
    """Serve synthesized MP3 speech stream."""
    path = Path("/tmp/jarvis_audio") / filename
    if path.exists():
        return send_file(str(path), mimetype="audio/mpeg")
    return ("Audio file not found", 404)


@app.route("/api/earcon/<name>", methods=["GET"])
def api_earcon(name):
    """Serve instant earcons (chime_ping, chime_listen)."""
    p = Path.home() / ".jarvis" / "audio_cache" / f"{name}.wav"
    if p.exists():
        return send_file(str(p), mimetype="audio/wav")
    return ("Earcon not found", 404)


@app.route("/api/tts", methods=["GET"])
def api_tts():
    """Synthesize any provided text into streaming MP3."""
    text = request.args.get("text", "Greetings Sir.").strip()

    # Instant check for pre-cached audio
    cache_dir = Path.home() / ".jarvis" / "audio_cache"
    if "yes, sir" in text.lower() and (cache_dir / "ack_yes_sir.mp3").exists():
        return send_file(str(cache_dir / "ack_yes_sir.mp3"), mimetype="audio/mpeg")

    audio_cache_dir = Path("/tmp/jarvis_audio")
    audio_cache_dir.mkdir(parents=True, exist_ok=True)
    filename = f"tts_{int(time.time()*1000)}.mp3"
    audio_file_path = audio_cache_dir / filename

    async def synth():
        comm = edge_tts.Communicate(text, voice=VOICE_NAME, rate="+20%")
        await asyncio.wait_for(comm.save(str(audio_file_path)), timeout=8.0)

    try:
        asyncio.run(synth())
        return send_file(str(audio_file_path), mimetype="audio/mpeg")
    except Exception as e:
        return (f"TTS error: {e}", 500)


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Transcribe audio upload (WAV/WebM) via Groq Whisper API."""
    if "file" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["file"]
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (audio_file.filename, audio_file.read(), audio_file.content_type)}
    data = {"model": "whisper-large-v3-turbo", "language": "en"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(GROQ_WHISPER_URL, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                transcript = resp.json().get("text", "")
                return jsonify({"transcript": transcript})
            return jsonify({"error": resp.text}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qr", methods=["GET"])
def api_qr():
    """Return PNG QR code pointing to this server."""
    lan_ip = get_lan_ip()
    url = f"http://{lan_ip}:{PORT}"
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


def render_terminal_qr():
    """Print ASCII QR Code directly in terminal."""
    lan_ip = get_lan_ip()
    http_url = f"http://{lan_ip}:{PORT}"
    https_url = f"https://{lan_ip}:8766"

    qr = qrcode.QRCode(box_size=1, border=1)
    qr.add_data(https_url)
    qr.make(fit=True)

    print("\n" + "═" * 58)
    print("✦ J.A.R.V.I.S. LOCAL NETWORK HUB — SCAN TO CONNECT")
    print("═" * 58)
    print(f"HTTP:  \033[1;36m{http_url}\033[0m")
    print(f"HTTPS: \033[1;32m{https_url}\033[0m (Secure Context for Phone Microphone)\n")
    qr.print_ascii(invert=True)
    print("═" * 58)
    print("Open either URL on your phone or tablet on the same Wi-Fi.")
    print("HTTPS (port 8766) unlocks native phone microphone support.\n")


def main():
    lan_ip = get_lan_ip()
    render_terminal_qr()

    ssl_cert = Path.home() / ".jarvis" / "ssl" / "cert.pem"
    ssl_key = Path.home() / ".jarvis" / "ssl" / "key.pem"
    if ssl_cert.exists() and ssl_key.exists():
        def run_https():
            try:
                print(f"✦ Secure Context HTTPS Hub listening on https://0.0.0.0:8766\n")
                app.run(host="0.0.0.0", port=8766, debug=False, threaded=True, ssl_context=(str(ssl_cert), str(ssl_key)))
            except Exception as e:
                print(f"HTTPS Hub error: {e}")

        t = threading.Thread(target=run_https, daemon=True)
        t.start()

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
