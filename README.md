# ✦ J.A.R.V.I.S. — Tactical AI Terminal & Local Network Smart Hub

> **Just A Rather Very Intelligent System**  
> Groq Hardware-Accelerated Conversational Intelligence, British Neural TTS, Continuous "Hey Jarvis" Wake-Word Voice Recognition, and Multi-Device LAN Control.

---

## ⚡ Overview

J.A.R.V.I.S. is an open-source personal AI assistant built for Linux, bringing the authentic J.A.R.V.I.S. experience to your desktop and phone.

- **Groq Cloud Acceleration**: Powered by `qwen/qwen3.8-27b` with **~65ms** turnaround.
- **Offline Resilience**: Auto-failover to local Ollama (`qwen2.5-coder:1.5b`) when disconnected.
- **Neural Voice Synthesis (TTS)**: Microsoft Edge Neural TTS configured with **`en-GB-RyanNeural`** played non-blockingly via PipeWire (`pw-play`).
- **Continuous "Hey Jarvis" Wake-Word**:
  - **On Mobile**: Continuous browser Web Speech API listening from your phone or tablet on the same Wi-Fi.
  - **On Laptop**: Background daemon monitoring physical microphone via PipeWire and Groq Whisper.
- **Autonomous Desktop Pop-up**: Saying *"Hey Jarvis"* automatically launches the terminal HUD on your screen even if J.A.R.V.I.S. was closed.
- **Local Network Hub**: Responsive mobile OLED Web HUD running on `http://0.0.0.0:8765` with terminal QR code pairing.
- **Remote Hardware Controls**: Control master volume, mute, purge RAM cache, lock screen, and trigger GitHub sync from your phone.

---

## 🚀 Quick Start

### 1. Terminal Interface
```bash
jarvis
```

### 2. Pair Mobile Phone / Other LAN Devices
```bash
jarvis qr
```
Scan the ASCII QR code with your phone camera or visit `http://<laptop-ip>:8765`.

### 3. Background Wake-Word Listener
```bash
jarvis listen
```
*(Also managed via `systemctl --user {start|stop|status} jarvis-listener.service`)*

---

## 🛠️ Architecture

- `jarvis.py`: Interactive terminal TUI with rich telemetry, completions, and slash commands.
- `jarvis_server.py`: Flask network hub serving the mobile OLED HUD, TTS streaming, and REST API.
- `jarvis_listener.py`: Continuous physical microphone wake-word spotting daemon.
- `jarvis-server.service` & `jarvis-listener.service`: Systemd user daemons for 24/7 background availability.

---

## 📜 License
MIT License. Created by [w0tu](https://github.com/w0tu).
