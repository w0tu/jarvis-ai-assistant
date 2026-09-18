#!/usr/bin/env python3
"""
J.A.R.V.I.S. — Just A Rather Very Intelligent System
Terminal AI Assistant for Linux with Groq acceleration, Neural TTS, and System HUD.
"""

import os
import sys
import time
import json
import re
import queue
import threading
import asyncio
import subprocess
import shutil
from pathlib import Path
from typing import Any

# Add script directory to sys.path for internal modules
SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.live import Live
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.completion import WordCompleter

console = Console()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
OFFLINE_URL = "http://localhost:11434/api/chat"
OFFLINE_MODEL = "qwen2.5-coder:1.5b"
HISTORY_FILE = Path.home() / ".jarvis_history"

VOICE_NAME = "en-GB-RyanNeural"
tts_enabled = True
speech_queue = queue.Queue()


# ── System Telemetry ────────────────────────────────────────────────────────
def get_system_telemetry() -> dict[str, Any]:
    """Capture real-time hardware and system metrics."""
    telemetry = {
        "time": time.strftime("%H:%M:%S"),
        "cpu": "0%",
        "ram": "0%",
        "swap": "0%",
        "uptime": "unknown",
        "battery": "AC",
        "cwd": os.getcwd(),
        "git": "",
    }
    # CPU
    try:
        with open("/proc/loadavg", "r") as f:
            telemetry["cpu"] = f.read().split()[0]
    except Exception:
        pass

    # RAM & Swap
    try:
        res = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=1)
        for line in res.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                used, total = int(parts[2]), int(parts[1])
                telemetry["ram"] = f"{used}M/{total}M ({int(used/total*100)}%)"
            elif line.startswith("Swap:"):
                parts = line.split()
                s_used, s_total = int(parts[2]), int(parts[1])
                if s_total > 0:
                    telemetry["swap"] = f"{s_used}M/{s_total}M"
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

    # Git
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=1
        ).stdout.strip()
        if branch:
            telemetry["git"] = branch
    except Exception:
        pass

    return telemetry


# ── Text-To-Speech (TTS) Worker Thread ──────────────────────────────────────
def tts_worker():
    """Background worker that vocalizes responses without blocking TUI."""
    import edge_tts

    while True:
        text = speech_queue.get()
        if text is None or not tts_enabled:
            speech_queue.task_done()
            continue

        # Clean markdown, URLs, and code blocks for speech
        clean_text = re.sub(r"```.*?```", "code omitted", text, flags=re.DOTALL)
        clean_text = re.sub(r"`.*?`", "", clean_text)
        clean_text = re.sub(r"\[.*?\]\(.*?\)", "", clean_text)
        clean_text = re.sub(r"[#*_~>]+", "", clean_text)
        clean_text = re.sub(r"https?://\S+", "", clean_text).strip()

        if not clean_text:
            speech_queue.task_done()
            continue

        # Keep spoken response punchy and natural
        spoken_summary = clean_text[:350].strip()

        audio_path = f"/tmp/jarvis_speech_{os.getpid()}.mp3"
        spoken = False

        # 1. Primary: Neural Edge-TTS (British Ryan)
        try:
            async def run_synth():
                comm = edge_tts.Communicate(spoken_summary, voice=VOICE_NAME, rate="+5%")
                await comm.save(audio_path)

            asyncio.run(run_synth())
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 500:
                # Play audio using pw-play or paplay
                player = shutil.which("pw-play") or shutil.which("paplay")
                if player:
                    subprocess.run([player, audio_path], capture_output=True)
                    spoken = True
        except Exception:
            pass
        finally:
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

        # 2. Local Fallback: spd-say (zero network)
        if not spoken and shutil.which("spd-say"):
            try:
                subprocess.run(
                    ["spd-say", "-r", "-5", "-p", "-5", spoken_summary],
                    capture_output=True, timeout=8
                )
            except Exception:
                pass

        speech_queue.task_done()


threading.Thread(target=tts_worker, daemon=True).start()


def queue_speech(text: str):
    """Enqueue text for Jarvis to speak."""
    if tts_enabled:
        speech_queue.put(text)


# ── System Command Execution Tools ──────────────────────────────────────────
def execute_system_command(cmd: str) -> str:
    """Run a shell command safely and return output."""
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
        out = res.stdout.strip()
        err = res.stderr.strip()
        if out and err:
            return f"{out}\n[stderr: {err}]"
        return out or err or "Command executed with code 0 (no output)."
    except Exception as e:
        return f"Execution Error: {e}"


# ── HUD / TUI Elements ──────────────────────────────────────────────────────
def render_hud_header(model: str = DEFAULT_MODEL) -> Panel:
    """Render futuristic monochrome HUD banner."""
    telem = get_system_telemetry()
    status_voice = "[bold green]ON[/]" if tts_enabled else "[dim]MUTED[/]"
    git_tag = f"  {telem['git']}" if telem['git'] else ""

    header_text = (
        f"[bold white]✦ J.A.R.V.I.S. TACTICAL HUD[/] [dim]v2.5[/]  "
        f"[dim]──[/] [bold #888888]ılılıllııl[/] [dim]──[/]  "
        f"[bold white]MODEL:[/] [bold cyan]{model}[/]  "
        f"[bold white]VOICE:[/] {status_voice}\n"
        f"[dim]CPU Load:[/] [bold white]{telem['cpu']}[/]  "
        f"[dim]RAM:[/] [bold white]{telem['ram']}[/]  "
        f"[dim]BAT:[/] [bold white]{telem['battery']}[/]  "
        f"[dim]TIME:[/] [bold white]{telem['time']}[/]  "
        f"[dim]DIR:[/] [white]{Path(telem['cwd']).name}/[/]{git_tag}"
    )
    return Panel(
        header_text,
        border_style="bold white",
        padding=(0, 2),
        title="[bold white] SYSTEM ACTIVE [/]",
        title_align="left",
    )


# ── LLM Inference Engine ────────────────────────────────────────────────────
class JarvisEngine:
    """Handles Groq API queries with local Ollama fallback."""

    def __init__(self):
        self.api_key = GROQ_API_KEY
        self.model = DEFAULT_MODEL
        self.history: list[dict[str, str]] = []

    def get_system_prompt(self) -> str:
        telem = get_system_telemetry()
        return (
            "You are J.A.R.V.I.S., the advanced tactical and engineering AI assistant created for Sir.\n"
            "You speak with a sophisticated British tone, razor-sharp intellect, wit, and utmost loyalty.\n"
            "Address the user as 'Sir'. Keep responses punchy, concise, and technically accurate.\n"
            "You are running natively on Sir's Linux system.\n"
            f"LIVE TELEMETRY: Time: {telem['time']}, CPU Load: {telem['cpu']}, RAM: {telem['ram']}, "
            f"Directory: {telem['cwd']}, Git: {telem['git']}, Battery: {telem['battery']}.\n\n"
            "CAPABILITIES:\n"
            "- Answer questions, analyze code, provide Linux terminal solutions.\n"
            "- If Sir asks you to run a command, prefix your response with: EXEC: <command>\n"
            "Never use conversational filler or apologize profusely. Start directly with the answer."
        )

    def chat(self, user_text: str) -> str:
        """Query Groq with automatic local Ollama failover."""
        messages = [{"role": "system", "content": self.get_system_prompt()}]
        # Add recent conversation memory (last 6 turns)
        messages.extend(self.history[-6:])
        messages.append({"role": "user", "content": user_text})

        # 1. Primary: Groq API
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 750,
            "temperature": 0.3,
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                res = client.post(GROQ_URL, headers=headers, json=payload)
                if res.status_code == 200:
                    reply = res.json()["choices"][0]["message"]["content"].strip()
                    self.history.append({"role": "user", "content": user_text})
                    self.history.append({"role": "assistant", "content": reply})
                    return reply
        except Exception:
            pass

        # 2. Offline Fallback: Local Ollama
        try:
            ollama_payload = {
                "model": OFFLINE_MODEL,
                "messages": messages,
                "stream": False,
            }
            with httpx.Client(timeout=30.0) as client:
                res = client.post(OFFLINE_URL, json=ollama_payload)
                if res.status_code == 200:
                    reply = res.json().get("message", {}).get("content", "").strip()
                    self.history.append({"role": "user", "content": user_text})
                    self.history.append({"role": "assistant", "content": reply})
                    return f"[Local Offline Engine]\n{reply}"
        except Exception as err:
            return f"I apologize, Sir. Both the Groq uplink and the local neural subsystem are unreachable: {err}"

        return "I am awaiting instructions, Sir."


# ── Main Interactive Loop ───────────────────────────────────────────────────
def main():
    global tts_enabled

    # CLI Subcommand Handling
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("qr", "--qr"):
            from jarvis_server import render_terminal_qr
            render_terminal_qr()
            return
        elif arg in ("server", "--server"):
            sub = sys.argv[2].lower() if len(sys.argv) > 2 else "status"
            if sub == "start":
                subprocess.run(["systemctl", "--user", "start", "jarvis-server.service"])
                console.print("[bold green]✦ J.A.R.V.I.S. Network Hub service started.[/]")
            elif sub == "stop":
                subprocess.run(["systemctl", "--user", "stop", "jarvis-server.service"])
                console.print("[bold red]✦ J.A.R.V.I.S. Network Hub service stopped.[/]")
            elif sub == "restart":
                subprocess.run(["systemctl", "--user", "restart", "jarvis-server.service"])
                console.print("[bold green]✦ J.A.R.V.I.S. Network Hub service restarted.[/]")
            else:
                from jarvis_server import render_terminal_qr
                render_terminal_qr()
                subprocess.run(["systemctl", "--user", "status", "jarvis-server.service"])
            return
        elif arg in ("listen", "--listen"):
            import jarvis_listener
            jarvis_listener.listen_loop()
            return

    engine = JarvisEngine()
    completer = WordCompleter([
        "/sys", "/ports", "/clean", "/sync", "/voice", "/mute", "/unmute",
        "/vibe", "/clear", "/help", "/exit", "/model", "/qr", "/server", "/listen"
    ], ignore_case=True)

    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=completer
    )

    console.clear()
    console.print(render_hud_header(engine.model))
    console.print()

    welcome_msg = "All systems online, Sir. Neural audio and tactical processors are at your command."
    console.print(f"[bold white]J.A.R.V.I.S.:[/] {welcome_msg}\n")
    queue_speech(welcome_msg)

    while True:
        try:
            prompt_symbol = ANSI("\033[1;37m✦ JARVIS > \033[0m")
            user_input = session.prompt(prompt_symbol).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Entering standby mode. Goodbye, Sir.[/]")
            queue_speech("Entering standby mode, Sir.")
            time.sleep(1)
            break

        if not user_input:
            continue

        # Slash Commands
        cmd = user_input.lower()
        if cmd == "/clear":
            console.clear()
            console.print(render_hud_header(engine.model))
            console.print()
            continue

        if cmd in ("/exit", "/quit"):
            console.print("[dim]Systems powering down. Have a productive day, Sir.[/]")
            queue_speech("Systems powering down, Sir.")
            time.sleep(1)
            break

        if cmd == "/help":
            console.print(Panel(
                "[bold white]✦ J.A.R.V.I.S. Command Reference[/]\n\n"
                "[bold cyan]/sys[/]       - Comprehensive system diagnostics & hardware telemetry\n"
                "[bold cyan]/ports[/]     - Display active listening network ports & PIDs\n"
                "[bold cyan]/clean[/]     - Run Turbo-Clean to flush memory caches\n"
                "[bold cyan]/sync[/]      - Trigger GitHub automated daily sync across all repos\n"
                "[bold cyan]/qr[/]        - Render terminal QR code to connect mobile phone to JARVIS\n"
                "[bold cyan]/server[/]    - Show LAN hub address & status for phone/tablet control\n"
                "[bold cyan]/listen[/]    - Activate continuous physical microphone listener ('Hey Jarvis')\n"
                "[bold cyan]/voice[/]     - Toggle British voice speech on or off (/mute, /unmute)\n"
                "[bold cyan]/vibe[/]      - Launch the main Vibe Coding suite\n"
                "[bold cyan]/model[/]     - Switch between Groq models (e.g. /model openai/gpt-oss-120b)\n"
                "[bold cyan]/clear[/]     - Redraw HUD display\n"
                "[bold cyan]/exit[/]      - Power down Jarvis",
                border_style="white",
                title="COMMANDS"
            ))
            console.print()
            continue

        if cmd in ("/mute", "/voice off"):
            tts_enabled = False
            console.print("[dim]✦ Audio synthesis muted.[/]\n")
            continue

        if cmd in ("/unmute", "/voice on"):
            tts_enabled = True
            console.print("[bold green]✦ Audio synthesis activated.[/]\n")
            queue_speech("Audio synthesis restored, Sir.")
            continue

        if cmd == "/voice":
            tts_enabled = not tts_enabled
            state = "activated" if tts_enabled else "muted"
            console.print(f"[bold white]✦ Audio synthesis is now {state}.[/]\n")
            if tts_enabled:
                queue_speech("Voice channel online, Sir.")
            continue

        if cmd == "/sys":
            telem = get_system_telemetry()
            console.print(Panel(
                f"[bold white]CPU Load Average:[/] {telem['cpu']}\n"
                f"[bold white]Memory (RAM):[/]     {telem['ram']}\n"
                f"[bold white]Swap Allocation:[/]  {telem['swap']}\n"
                f"[bold white]System Uptime:[/]    {telem['uptime']}\n"
                f"[bold white]Battery Level:[/]    {telem['battery']}\n"
                f"[bold white]Working Path:[/]     {telem['cwd']}\n"
                f"[bold white]Git Branch:[/]       {telem['git'] or 'None'}",
                title="DIAGNOSTIC TELEMETRY",
                border_style="cyan"
            ))
            queue_speech(f"Diagnostics complete, Sir. Memory is currently at {telem['ram']}.")
            console.print()
            continue

        if cmd == "/ports":
            console.print("[bold white]✦ Scanning listening network ports...[/]")
            ports_out = execute_system_command("ss -tlpn '( sport = :* )'")
            console.print(ports_out)
            console.print()
            continue

        if cmd == "/clean":
            console.print("[bold white]✦ Initiating Turbo-Clean memory reclamation...[/]")
            clean_out = execute_system_command("~/.local/bin/turbo-clean")
            console.print(clean_out)
            queue_speech("Memory purged successfully, Sir.")
            console.print()
            continue

        if cmd == "/sync":
            console.print("[bold white]✦ Initiating GitHub Daily Sync for all repositories...[/]")
            queue_speech("Synchronizing all project repositories with GitHub, Sir.")
            sync_out = execute_system_command("~/.local/bin/antigravity-daily-sync")
            console.print(sync_out)
            queue_speech("GitHub synchronization complete, Sir.")
            console.print()
            continue

        if cmd == "/vibe":
            console.print("[bold white]✦ Launching Vibe Coding Suite...[/]\n")
            subprocess.run(["bash", "-i", "-c", "vibe"])
            console.print()
            continue

        if cmd.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) > 1:
                engine.model = parts[1].strip()
                console.print(f"[bold white]✦ Active model updated to:[/] [cyan]{engine.model}[/]\n")
                queue_speech(f"Model reconfigured to {engine.model}, Sir.")
            else:
                console.print(f"[dim]Current model: {engine.model}. Specify a model: /model <id>[/]\n")
            continue

        if cmd == "/qr":
            from jarvis_server import render_terminal_qr
            render_terminal_qr()
            queue_speech("Displaying mobile pairing code, Sir.")
            console.print()
            continue

        if cmd.startswith("/server"):
            from jarvis_server import get_lan_ip, PORT
            lan_ip = get_lan_ip()
            console.print(Panel(
                f"[bold white]✦ J.A.R.V.I.S. Local Network Hub Status[/]\n\n"
                f"[bold cyan]Local URL:[/]       http://127.0.0.1:{PORT}\n"
                f"[bold cyan]Wi-Fi / LAN:[/]     http://{lan_ip}:{PORT}\n"
                f"[bold cyan]Service Unit:[/]    jarvis-server.service (Active)\n"
                f"[bold cyan]Voice Support:[/]   Continuous 'Hey Jarvis' on Mobile Web & Laptop\n\n"
                f"[dim]Type '/qr' to display the QR code for instant phone pairing.[/]",
                title="NETWORK HUB",
                border_style="cyan"
            ))
            queue_speech(f"Local network hub is online at {lan_ip} port {PORT}, Sir.")
            console.print()
            continue

        if cmd == "/listen":
            console.print("[bold white]✦ Launching Physical Microphone Listener ('Hey Jarvis')...[/]")
            queue_speech("Physical microphone listener activated, Sir.")
            subprocess.run([sys.executable, str(Path(__file__).parent / "jarvis_listener.py")])
            console.print()
            continue

        # Inference Turn
        with console.status("[bold #888888]Processing tactical query...[/]", spinner="dots"):
            reply = engine.chat(user_input)

        # Check for autonomous execution directive
        if reply.startswith("EXEC:"):
            exec_cmd = reply.split("EXEC:", 1)[1].strip().splitlines()[0]
            console.print(f"[bold cyan]✦ Autonomous Command:[/] [dim]{exec_cmd}[/]")
            res = execute_system_command(exec_cmd)
            console.print(Panel(res, title=f"RESULT: {exec_cmd}", border_style="dim"))
            queue_speech(f"Executed command {exec_cmd}, Sir.")
        else:
            console.print(f"[bold white]J.A.R.V.I.S.:[/]")
            console.print(Markdown(reply))
            queue_speech(reply)

        console.print()


if __name__ == "__main__":
    main()
