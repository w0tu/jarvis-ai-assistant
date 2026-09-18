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

app = Flask(__name__)
CORS(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
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
      background: var(--accent-dim);
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: 0 0 12px var(--pulse-glow);
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
</head>
<body>

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
    <button class="btn-icon btn-mic" id="mic-btn" onclick="toggleContinuousListening()" title="Toggle 'Hey Jarvis' Voice Recognition">🎙️</button>
    <input type="text" id="query-input" placeholder="Type or say 'Hey Jarvis'..." autocomplete="off" onkeydown="if(event.key==='Enter') submitText()">
    <button class="btn-icon btn-send" onclick="submitText()">➤</button>
  </div>

  <audio id="tts-player" style="display:none;"></audio>

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

    // ── Continuous "Hey Jarvis" Voice Recognition (Web Speech API) ───────
    let recognition = null;
    let isListening = false;
    let isExecuting = false;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (SpeechRecognition) {
      recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = 'en-US';

      recognition.onstart = () => {
        isListening = true;
        micBtn.classList.add('active');
        wakeDot.classList.add('listening');
        wakeText.textContent = '✦ Listening: Say "Hey Jarvis <command>"';
      };

      recognition.onend = () => {
        // Auto-restart continuous listening if still toggled active
        if (isListening) {
          try { recognition.start(); } catch (e) {}
        } else {
          micBtn.classList.remove('active');
          wakeDot.classList.remove('listening');
          wakeText.textContent = 'Voice Paused. Tap mic to listen.';
        }
      };

      recognition.onresult = (event) => {
        let transcript = '';
        for (let i = event.resultIndex; i < event.results.length; ++i) {
          transcript += event.results[i][0].transcript;
        }
        transcript = transcript.trim().toLowerCase();

        // Check for Wake Word: "hey jarvis" or "jarvis"
        const wakeWordRegex = /\\b(hey\\s+jarvis|jarvis)\\b/i;
        if (wakeWordRegex.test(transcript) && !isExecuting) {
          // Extract command after wake word
          let cleanCmd = transcript.replace(/.*?\\b(hey\\s+jarvis|jarvis)[,:]?\\s*/i, '').trim();
          if (cleanCmd.length > 2) {
            isExecuting = true;
            wakeText.textContent = `✦ Triggered: "${cleanCmd}"`;
            sendPrompt(cleanCmd);
            setTimeout(() => { isExecuting = false; }, 3000);
          }
        }
      };

      recognition.onerror = (e) => {
        if (e.error !== 'no-speech') {
          console.warn('Speech recognition notice:', e.error);
        }
      };
    } else {
      wakeText.textContent = 'Voice API not natively supported on this browser. Type below.';
    }

    function toggleContinuousListening() {
      if (!recognition) {
        alert('Web Speech API is not supported in this browser. Please use Chrome/Edge or Safari.');
        return;
      }
      if (isListening) {
        isListening = false;
        recognition.stop();
      } else {
        try {
          recognition.start();
        } catch (e) {
          console.error(e);
        }
      }
    }

    // ── Audio Waveform Visualizer Canvas ──────────────────────────────────
    const canvas = document.getElementById('visualizer');
    const ctx = canvas.getContext('2d');
    let animId = null;

    function resizeCanvas() {
      canvas.width = canvas.clientWidth * window.devicePixelRatio;
      canvas.height = canvas.clientHeight * window.devicePixelRatio;
    }
    window.addEventListener('resize', resizeCanvas);
    resizeCanvas();

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
          // Flatten line when quiet
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


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Process text or voice prompt, trigger autonomous actions, return response + audio."""
    data = request.get_json(force=True, silent=True) or {}
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"text": "Awaiting your command, Sir.", "audio_url": None})

    # Autonomous action spotting
    prompt_lower = prompt.lower()
    executed_notice = ""
    if "volume up" in prompt_lower or "louder" in prompt_lower:
        execute_device_action("volume_up")
        executed_notice = "[Action: Increased volume by 10%]\n"
    elif "volume down" in prompt_lower or "quieter" in prompt_lower:
        execute_device_action("volume_down")
        executed_notice = "[Action: Decreased volume by 10%]\n"
    elif "mute" in prompt_lower:
        execute_device_action("volume_mute")
        executed_notice = "[Action: Toggled mute]\n"
    elif "clean" in prompt_lower and ("memory" in prompt_lower or "ram" in prompt_lower or "cache" in prompt_lower):
        res = execute_device_action("clean_memory")
        executed_notice = f"[Action: {res.get('message', 'Cleaned')}]\n"
    elif "lock" in prompt_lower and ("screen" in prompt_lower or "pc" in prompt_lower or "laptop" in prompt_lower):
        execute_device_action("lock_screen")
        executed_notice = "[Action: Locked session]\n"

    # Free Public APIs Direct Integration
    sys.path.insert(0, "/home/feds/.gemini/antigravity/scratch/free-apis")
    try:
        from free_apis import query_live_api
        if "weather" in prompt_lower:
            loc = prompt_lower.replace("weather", "").replace("what's the", "").replace("what is the", "").replace("in", "").strip()
            res = query_live_api("weather", loc)
            spoken = f"Weather report: {res}, Sir."
            return jsonify({
                "text": spoken,
                "spoken": spoken,
                "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
            })
        elif any(k in prompt_lower for k in ("bitcoin", "crypto", "btc", "eth")):
            coin = "ethereum" if "eth" in prompt_lower else "bitcoin"
            res = query_live_api("crypto", coin)
            spoken = f"{res}, Sir."
            return jsonify({
                "text": spoken,
                "spoken": spoken,
                "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
            })
        elif "joke" in prompt_lower:
            res = query_live_api("joke")
            spoken = f"Here is one for you, Sir: {res}"
            return jsonify({
                "text": spoken,
                "spoken": spoken,
                "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
            })
        elif "quote" in prompt_lower or "inspiration" in prompt_lower:
            res = query_live_api("quote")
            spoken = f"{res}, Sir."
            return jsonify({
                "text": spoken,
                "spoken": spoken,
                "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
            })
        elif "my ip" in prompt_lower or "public ip" in prompt_lower:
            res = query_live_api("ip")
            spoken = f"{res}, Sir."
            return jsonify({
                "text": spoken,
                "spoken": spoken,
                "audio_url": f"/api/tts?text={urllib.parse.quote(spoken)}"
            })
    except Exception:
        pass

    full_answer, spoken = ask_ai(prompt)
    if executed_notice:
        full_answer = executed_notice + full_answer

    import urllib.parse
    audio_url = f"/api/tts?text={urllib.parse.quote(spoken)}"

    return jsonify({
        "text": full_answer,
        "spoken": spoken,
        "audio_url": audio_url,
    })


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
    url = f"http://{lan_ip}:{PORT}"
    qr = qrcode.QRCode(box_size=1, border=1)
    qr.add_data(url)
    qr.make(fit=True)

    print("\n" + "═" * 58)
    print("✦ J.A.R.V.I.S. LOCAL NETWORK HUB — SCAN TO CONNECT")
    print("═" * 58)
    print(f"URL: \033[1;36m{url}\033[0m\n")
    qr.print_ascii(invert=True)
    print("═" * 58)
    print("Open this URL on your phone or any device on Wi-Fi.")
    print("Continuous voice listening ('Hey Jarvis') will be active.\n")


def main():
    lan_ip = get_lan_ip()
    render_terminal_qr()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
