<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/logo.png">
    <img src="docs/logo-on-dark.png" width="96" alt="Sparky">
  </picture>
</p>

<h1 align="center">Sparky</h1>

<p align="center"><b>Your private AI assistant. 100% local.</b><br>
Chat, see images, generate pictures and videos with sound, build apps, ask your documents, dictate and listen —<br>
everything runs on your own PC. Nothing is sent to the internet unless you turn it on.</p>

<p align="center">
  <img alt="Windows 10/11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6">
  <img alt="x64 and ARM64" src="https://img.shields.io/badge/x64%20%7C%20ARM64-NVIDIA%20CUDA-76B900">
  <img alt="100% local" src="https://img.shields.io/badge/100%25-local-8c7cf0">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="English and French" src="https://img.shields.io/badge/UI-English%20%7C%20Fran%C3%A7ais-lightgrey">
</p>

<p align="center"><img src="docs/demo-studio.gif" alt="Sparky Studio: videos with sound generated locally" width="900"><br><i>🎬 The Studio — every video here was generated on the PC, with its sound, by LTX-2.5.</i></p>

---

## ✨ What it does

### 💬 Chat
- 🔒 **Local chat history** — temporary chats keep their own transient files and support text and vision. Use a regular chat for image generation or editing, because the shared ComfyUI engine retains inputs and outputs.
- ✏️ **Edit** any message, 🔁 **regenerate** or ➡️ **continue** an answer, and flip between versions (‹ 2/3 ›).
- Open a separate conversation from a message to try another approach. Generated files follow the selected version. Use **Stop** to interrupt a running turn.
- 🧠 **Adjustable thinking** — Off, Light, Normal or Deep, and see how the model reasoned.
- 📝 **Rich answers** — tables, LaTeX formulas, Mermaid diagrams, task lists, syntax-highlighted code (all offline).
- 👁️ **Vision** — drop photos, screenshots or scans: the assistant sees and analyses them.
- 📎 **Attach any file** — PDF, Word, PowerPoint, Excel, text, code, audio and video (transcribed offline).
- 🗂️ **Organize your chats** — full-text search, rename, 📌 pin, 📁 folders, export (JSON / Markdown) and import.

### 📚 Knowledge & web
- 📄 **Ask your documents (RAG)** — collections of PDFs, Office files, text, code… indexed on your PC, answers **with numbered sources** [1] [2].
- Link a local folder in **Workspace > Documents** to index additions, edits and removals. When upgrading an existing library, reselect the collections for each assistant or conversation that needs them. The library retains the documents.
- 🌐 **Web search, when you want it** — turn on *Web*: the assistant searches (DuckDuckGo or your SearXNG), reads pages and the links you paste, and cites them. Off by default.
- 🧑‍💼 **Assistants** — a role, its documents and web access, reusable in one click.
- ⚡ **Prompt library** — type **/** to insert a saved prompt with `{{variables}}`; standing instructions for every chat or just one.

### 🎨 Create
- 🎬 **Studio: images & videos** — a gallery of everything you generated, with a Grok-Imagine-like prompt bar: **text → image**, **text → video**, **image → video** ("Animate"). Videos come **with synchronized sound** 🔊 (LTX-2.5), and your idea is expanded into a detailed prompt first.
- 🖼️ **Images** — generate and edit by asking ("make it snow", "remove the bottle"); the agent checks its own result and retries.
- 🧩 **Code & apps panel** — games, pages, tools and scripts written live next to the chat, with a **running preview**.
- 🏗️ **Multi-file projects** — build websites, Python or Node.js projects with generated assets. Sparky checks source syntax and local resource references, then sends errors back to the assistant for repair. Preview a website on the isolated origin, rerun the checks, or download the project as a ZIP. Native execution and package installation are disabled in the checker.

### 🎙️ Voice
- 🎤 **Dictation, offline** — click the mic, talk, stop: Whisper writes it in the input box.
- 🔊 **Read aloud** — on any answer, offline Piper voices (English, French, German, Spanish, Italian).

### 🛠️ Tools & automation
- 💻 **Computer mode** — Sparky can look at and act on your PC: browse folders and the desktop, read and search files (PDF and Office too), take a screenshot, write, copy, move, send to the Recycle Bin, open files and apps, run PowerShell commands. **You choose the security level** for each chat: *Off*, *Read only*, *Read freely & ask before changes* (recommended), *Ask for everything* or *Full access*; actions that need your OK show an **approval card** (Allow / Always in this chat / Deny).
- 🔌 **MCP tools** — plug any Model Context Protocol server (same config as Claude Desktop: files, GitHub, databases…).
- ⏰ **Scheduled tasks** — "every morning at 8, summarize the tech news": the answer lands in a new chat.
- In **Workspace > Tasks**, select a model and enable Web for tasks that need it. Choosing a collection runs a read-only document search and cited summary. Review the resulting conversation and approve or reject the summary before the next run. A pinned model must be active when its task runs.
- 🔗 **OpenAI-compatible API** — use Sparky's model from Continue, VS Code extensions or scripts at `http://127.0.0.1:7860/v1`. Off by default.
- 🤖 **Sparky Bot (beta)** — a companion that remembers you and learns your style, with a memory you can see and edit ([details](#-sparky-bot--personal-companion-with-memory-beta-in-progress)).

### ⚙️ Models & setup
- 🩺 **System scan** — checks your GPU, memory and disk, then **recommends the models that fit your machine**.
- 📦 **Model library** — one-click installs, fast parallel resumable downloads, speed / quality / memory indicators *for your hardware*.
- ♻️ **Models already on your PC** — LM Studio, Ollama, Hugging Face cache, Jan, GPT4All, other ComfyUI installs… used in one click.
- ☁️ **Cloud models, if you want** — OpenRouter, Claude, OpenAI, Gemini, Mistral… Off by default.
- ✅ **Only official models** — "uncensored", "abliterated" or NSFW variants are excluded from the catalog and the Hugging Face search.
- 🌗 **Light or dark** theme, 🇬🇧 **English or 🇫🇷 French** interface, ⬆️ one-click updates.

## 📋 Everything Sparky supports

| Area | Supported |
|---|---|
| 💬 **Chat** | streaming answers · visible reasoning with 4 thinking levels · edit a sent message · regenerate · continue · answer versions (‹ 2/3 ›) · copy · read aloud · temporary chats · English / French UI · light / dark / auto theme |
| 🗂️ **Conversations** | history · full-text search · rename · pin · folders · export JSON (with images) or Markdown · import (Sparky JSON or `[{role, content}]`) |
| 🧑‍💼 **Setup of a chat** | assistant presets (role, documents, web) · per-chat instructions (system prompt) · standing instructions for every chat · prompt library with `/commands` and `{{variables}}` |
| 📄 **Documents (RAG)** | collections · PDF (with page numbers), Word, PowerPoint, Excel, OpenDocument, text, Markdown, CSV, JSON, code, audio and video (transcribed) · local multilingual embeddings (`multilingual-e5-small`) + keyword search · numbered citations with the source passages |
| 🌐 **Web** | search (DuckDuckGo / Bing, or your SearXNG) · reads pages and pasted links (HTML, PDF, text) · citations · private addresses blocked · off by default, per chat |
| 📎 **Files in a message** | images (vision), PDF, Office, text and code, audio and video (offline Whisper transcription) · drag and drop, paste |
| 🎬 **Studio** | media library of every image and video (Studio and chats) · text → image · text → video · image → video ("Animate") · 16:9, 9:16, 1:1, 4:3, 3:4 · 3 to 10 s · prompt enhancement · video player, download, reuse prompt · the local assistant pauses during a video and comes back after |
| 🎞️ **Video models** | LTX-2.5 22B (video + sound, ~40 s for 4 s of video on an RTX Spark once loaded) · Wan 2.2 5B (lighter, no sound) |
| 🖼️ **Images** | generate and edit by instruction (FLUX.2 Klein, Qwen-Image 2.1, SDXL) · paint a zone to edit · reuse a face, character, style or scene · the agent checks its result and retries |
| 🧩 **Code & projects** | isolated web previews, versions and downloads · multi-file projects (web, Python, Node.js) · automatic static checks with repair feedback · ZIP export with generated assets |
| 📝 **Markdown** | GFM tables · task lists · LaTeX (KaTeX) · Mermaid diagrams · syntax highlighting · citations · served locally (no CDN) |
| 🎙️ **Voice** | offline dictation (Whisper large-v3 turbo / base) with live level bars · offline read-aloud (Piper voices: en, fr, de, es, it) |
| 💻 **Computer mode** | list, read (PDF / Office), search, info, screenshot · write, mkdir, copy, move · Recycle Bin (never a permanent delete) · open files / apps / URLs · PowerShell commands (2 min timeout) · 5 security levels per chat · approval cards (once / always in this chat / deny) · system folders always protected |
| 🔌 **Tools** | MCP servers over stdio (`command` / `args` / `env`) or Streamable HTTP (`url` / `headers`), Claude Desktop config format |
| ⏰ **Automation** | scheduled tasks: daily, weekly, every N minutes, once · results filed in a new chat with an unread badge |
| 🔗 **API** | OpenAI-compatible `/v1/models`, `/v1/chat/completions` (streaming or not), `/v1/embeddings` · bearer key · off by default · local only |
| 📦 **Models** | local GGUF assistants (llama.cpp: CUDA x64 / ARM64, or CPU) · catalog with speed / quality / memory for your PC · models already on your PC (LM Studio, Ollama, Hugging Face cache…) · Hugging Face search · optional cloud models (OpenRouter, Claude, OpenAI, Gemini, Mistral, Groq, DeepSeek, xAI, any OpenAI-compatible server) · only official, non-NSFW models |
| 🤖 **Sparky Bot (beta)** | companion with long-term, editable memory · style profile · journal · relationship level |
| 🚀 **Install & updates** | step-by-step installer · system scan with recommended models · one-click updates |

## 📸 Screenshots

| | |
|---|---|
| <img src="docs/computer.png" alt="Computer mode with an approval card"><br>💻 **Computer mode** — Sparky reads your files, then asks before writing | <img src="docs/library-video.png" alt="Video models in the library"><br>📦 **Model library** — assistants, images, videos, voice |
| <img src="docs/studio.png" alt="Studio: videos generated locally"><br>🎬 **Studio** — your images and videos, with their sound | <img src="docs/studio-video.png" alt="Studio video viewer"><br>🎞️ **Video viewer** — balloons over Cappadocia, made on the PC in 2 minutes |
| <img src="docs/studio-animate.png" alt="Animate a picture"><br>✨ **Image → video** — pick a picture, describe the motion and the sounds | <img src="docs/web-search.png" alt="Web search with citations"><br>🌐 **Web search** — the assistant searches, reads and cites its sources |
| <img src="docs/knowledge-chat.png" alt="Answers from your documents with citations"><br>📄 **Documents** — answers from your files, with numbered sources | <img src="docs/markdown.png" alt="Tables, formulas and diagrams"><br>📝 **Rich answers** — tables, LaTeX, Mermaid, versions of an answer |
| <img src="docs/workspace-documents.png" alt="Workspace: document collections"><br>📚 **Workspace** — document collections indexed on your PC | <img src="docs/workspace-assistants.png" alt="Workspace: assistants"><br>🧑‍💼 **Assistants** — a role, documents and web access, reusable |
| <img src="docs/prompts.png" alt="Prompt library"><br>⚡ **Prompt library** — type / to insert a saved prompt | <img src="docs/workspace-tools.png" alt="MCP tools"><br>🔌 **Tools (MCP)** — plug any MCP server |
| <img src="docs/workspace-tasks.png" alt="Scheduled tasks"><br>⏰ **Scheduled tasks** — prompts that run by themselves | <img src="docs/light.png" alt="Light theme"><br>🌗 **Light theme** — or dark, or follow Windows |
| <img src="docs/image-edit.png" alt="Image generation and editing"><br>🖼️ **Images** — generate, then edit by instruction | <img src="docs/thinking.png" alt="Visible reasoning"><br>🧠 **Thinking** — see how the model reasoned |
| <img src="docs/settings.png" alt="Settings"><br>⚙️ **Settings** — language, theme, system, models, context | <img src="docs/library.png" alt="Model library"><br>📦 **All models** — speed, quality and memory for your PC |
| <img src="docs/welcome.png" alt="Welcome screen with system scan"><br>🩺 **First launch** — system scan and recommended models | <img src="docs/picker.png" alt="Model menu with thinking levels"><br>🔀 **Model menu** — switch models, pick a thinking level |

<p align="center"><img src="docs/demo-artifact.gif" alt="Sparky writes a Breakout game live in the side panel, then runs it" width="800"><br><i>🧩 A Breakout game written live in the side panel, then played.</i></p>

<p align="center"><img src="docs/demo-image.gif" alt="Generating and editing an image" width="800"></p>

<p align="center"><img src="docs/demo-voice.gif" alt="Offline voice dictation" width="800"><br><i>Offline voice dictation: live level bars, then the text lands in the input box.</i></p>

## 🚧 Sparky Bot — personal companion with memory *(beta, in progress)*

> **Status: in active development.** Sparky Bot ships as a beta inside Sparky (sidebar → *Sparky Bot*). It works today, but the memory engine and the personality system are still being tuned and may change.

Sparky Bot is a friend-like companion that **remembers you, learns your style and grows with you** — in the spirit of OpenAI's *Dots* and Grok's companions, but **100% local** and with a memory you can actually see and edit.

<p align="center"><img src="docs/demo-bot.gif" alt="Sparky Bot learns about the user while chatting" width="900"></p>

| | |
|---|---|
| <img src="docs/bot.png" alt="Sparky Bot with its memory"><br>**Memory** — every fact it learned, grouped, editable, pinnable | <img src="docs/bot-journal.png" alt="Sparky Bot journal"><br>**Journal** — it reflects between conversations and remembers to follow up |

**How the memory works**

- **Learns after every message** — a background pass extracts durable facts about you (who you are, tastes, projects, people, goals, events) and decides to *add*, *update* or *forget*, Mem0-style.
- **Guard-rails in code, not just in the prompt** — one fact per memory; an update must be about the *same* fact (checked by meaning) or it becomes a new memory instead of overwriting; near-duplicates are ignored; a new memory must come from *your* words, never from the bot's own reply.
- **Smart retrieval** — before each reply it picks the memories that matter now: meaning (local multilingual embeddings, `multilingual-e5-small`), importance, recency, plus pinned facts.
- **Time-aware** — "next Tuesday" is stored as a real date, and past events are updated with what happened.
- **Learns your style** — tone, length, formality, humor, language.
- **Reflects when idle** — merges duplicates, writes a short journal, updates its summary of your relationship and prepares what to ask next time ("How did the meetup go?").
- **Relationship level** — from *Just met* to *Close friends* as you talk over days. Always a friend: never romantic, never clingy.
- **You stay in control** — see, edit, pin or delete any memory, turn learning off, or *Forget everything* in one click. Nothing leaves your PC.

**Roadmap**

- [x] Persistent memory with add / update / forget, guard-rails and semantic search
- [x] Style profile, journal, follow-ups, relationship level, 4 personalities
- [x] Animated avatar, bilingual UI (English / French)
- [ ] Voice conversations (Whisper in, local text-to-speech out; read-aloud already works in the main chat)
- [ ] Proactive check-ins (optional notifications)
- [ ] Memory import / export, per-memory "why do you know this?" with the source message
- [ ] Larger evaluation set for the memory engine across models

## 🚀 Install (Windows 10/11)

**One line**, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/anisayari/sparky/main/install.ps1 | iex
```

**Or** download the project (*Code > Download ZIP*), unzip it and double-click `install.bat`.

For a smaller chat installation, run `.\install.ps1 -ChatOnly` from the downloaded project. Run the installer again with the full profile to add Studio later.

The installer walks you through 7 simple steps:

1. choose your language (English / Français) and check your machine,
2. pick the install folder (default `%LOCALAPPDATA%\Sparky`),
3. install Python 3.12 if needed (via winget),
4. install PyTorch for your GPU and the image engine [ComfyUI](https://github.com/comfyanonymous/ComfyUI),
5. install the AI engine [llama.cpp](https://github.com/ggml-org/llama.cpp) (CUDA build for NVIDIA GPUs, x64 or ARM64),
6. scan your hardware and show the models recommended for it (nothing is downloaded yet),
7. create a **Sparky** shortcut on the desktop and in the Start menu, then open Sparky in your browser.

<p align="center"><img src="docs/installer.png" alt="The step-by-step installer" width="560"></p>

Sparky then opens at <http://127.0.0.1:7860> on its welcome screen: pick your language, see the system scan, use the models **already on your PC** in one click, or install the recommended ones (or any other from *See all models*). Nothing is downloaded without your click.

Next time, double-click the shortcut or `start.bat`. On Windows, `stop.bat` waits for this installation to exit. The server closes only the engines it started and their descendants. Engines started separately or by an older version can be closed in their original application.

Installer options: `-Lang en|fr`, `-Dir <folder>`, `-Yes` (no questions), `-Models` (also download the recommended models from the terminal), `-NoLaunch`. For the one-line install, use the environment variables `SPARKY_LANG`, `SPARKY_DIR`, `SPARKY_YES`, `SPARKY_MODELS`, `SPARKY_NOLAUNCH`.

### 💻 Which machine?

| Machine | Runs well |
|---|---|
| NVIDIA GPU 24 GB+ or 32 GB+ unified memory | 27–35B assistants, Qwen-Image 2.1 |
| NVIDIA GPU 12–16 GB | 9–20B assistants, FLUX.2 Klein |
| NVIDIA GPU 6–8 GB | 4–9B assistants, FLUX.2 Klein (slower) |
| No NVIDIA GPU | small assistants (4B) on the CPU, no image generation |

The system scan picks automatically among these.

## 🧠 Models

Speeds measured on an RTX Spark N1X (Q4, full power); Sparky adapts them to your GPU.

| Assistant | Size | Speed | GPQA (published) | Vision | Best for |
|---|---|---|---|---|---|
| gpt-oss 20B | 11 GB | 83 tok/s | 71.5 | | fastest |
| Qwen3.5 35B A3B | 21 GB | 74 tok/s | 84.2 | ✓ | best fast all-rounder |
| Nemotron 3.5 Lightning 30B | 24 GB | 74 tok/s | 75.4 | | fast without thinking |
| Qwen3.5 4B | 3 GB | 66 tok/s | 76.2 | ✓ | tiny, even without a GPU |
| Gemma 4 26B A4B | 17 GB | 62 tok/s | 82.3 | ✓ | fast and versatile |
| Qwen3.5 9B | 6 GB | 34 tok/s | 81.7 | ✓ | best small model |
| Gemma 4 12B | 7 GB | 34 tok/s | 78.8 | ✓ | light |
| Qwen3.8 27B | 16 GB | 16 (33 with MTP) | 89.2 | ✓ | smartest, best at code |
| Gemma 4 E4B | 6 GB | – | – | ✓ | small machines |

All of them can think before answering (adjustable level). Vision can be added in one click when it is missing.

| Images | Size | Time per image | Style |
|---|---|---|---|
| FLUX.2 Klein 4B | 7 GB | ~3 s | photo, readable text, edits by instruction |
| Qwen-Image 2.1 | 16 GB | ~35 s | most realistic, edits by instruction |
| Juggernaut XL v9 | 10 GB* | ~15 s | photo (SDXL) |
| Animagine XL 4.0 | 10 GB* | ~15 s | illustration (SDXL) |

| Voice | Size | Note |
|---|---|---|
| Whisper large-v3 turbo | 1.6 GB | very accurate, 99 languages (GPU) |
| Whisper base | 0.3 GB | light, for machines without a GPU |

| Video | Size | Time for 5 s | Note |
|---|---|---|---|
| LTX-2.5 22B (distilled, int8) | 39 GB | ~45 s once loaded (RTX Spark) | video **with sound**, text or image to video |
| Wan 2.2 TI2V 5B | 18 GB | ~2–3 min | lighter, no sound |

Video models are big: during a video, Sparky pauses the local assistant to give the whole GPU memory to the video model (about 5× faster), then reloads it.

\* includes the IP-Adapter files (≈ 4 GB, shared by all SDXL models) used to reuse a character or a style.

Every model keeps its own license (Apache 2.0, Gemma Terms of Use, NVIDIA Open Model License, etc.): check its Hugging Face page before any commercial use.

## 🔧 Existing installation (ComfyUI / llama.cpp already installed)

Create a `config.json` file at the project root to reuse your folders:

```json
{
  "python": "D:\\ai\\venv\\Scripts\\python.exe",
  "comfy_dir": "D:\\ai\\ComfyUI",
  "llama_server": "D:\\ai\\llama\\llama-server.exe",
  "llm_dir": "D:\\ai\\models",
  "mmproj_dir": "D:\\ai\\mmproj"
}
```

Other keys: `voice_dir`, `data_dir`, `app_port` (7860), `llm_port` (8080), `comfy_port` (8188). The `SPARKY_CONFIG` environment variable can point to another config file.

## ⌨️ Command line

```powershell
runtime\venv\Scripts\python.exe app\syscheck.py                  # system scan (add --lang=fr for French)
runtime\venv\Scripts\python.exe app\models_cli.py --list         # catalog
runtime\venv\Scripts\python.exe app\models_cli.py --recommended  # install the recommended models
runtime\venv\Scripts\python.exe app\models_cli.py qwen359        # install one model
```

## ⬆️ Update / uninstall

- **Update**: Sparky checks GitHub once an hour. When a new version is out, a message offers to **install the update**: Sparky runs `git pull` and restarts by itself. If files were modified locally, it lists them and gives the command to put them aside first; for an install made from the ZIP, it gives the install command to run again. The current version is in *Settings > Version* (and in the `VERSION` file).
- **Uninstall**: `stop.bat`, then delete the install folder (`%LOCALAPPDATA%\Sparky` by default) and the shortcuts.

## ☁️ Cloud models (optional)

Sparky is 100% local by default. If you want, *Settings > Cloud models* lets you add a key for **OpenRouter, Anthropic (Claude), OpenAI, Google Gemini, Mistral, Groq, DeepSeek, xAI**, or any **OpenAI-compatible** server (LM Studio, vLLM, a company proxy...). Pick a model in the provider's list: it appears in the model menu with a ☁, and switching to it frees your GPU.

- Keys stay on your PC (`data\cloud.json`) and are never sent back to the page.
- While a cloud model is active, the footer says so: your messages, images and attached files then go to that provider.
- Claude goes through the official `anthropic` SDK (adaptive thinking, effort, refusal fallback); the others through their OpenAI-compatible API.

## 🔒 Privacy

Everything runs on your machine: conversations, documents, images and models stay in the install folder (`data\`, `models\`). The only outgoing connections download the tools and models (GitHub, PyPI, Hugging Face, the Piper voices), search the model library, check for updates (the `VERSION` file on GitHub), and — only when you turn them on — search the web and read pages (*Web* chip of a chat, scheduled tasks), run MCP tools that reach the internet, or talk to a cloud provider.

The management API verifies that the local client belongs to the same OS account and checks the browser origin. Web previews use a separate port. The optional OpenAI-compatible API (`/v1`) requires its bearer key and listens on `127.0.0.1`. Public web requests connect to a validated public IP at each redirect.

After upgrading from 1.6.1 or earlier, select the assistant, document collections, web access and Computer permissions again for existing conversations. Older session files do not record whether these settings were chosen locally or imported from someone else's file.

**Local-only companion** in the companion settings requires an active local model for that companion. With a cloud model and cloud access enabled for the companion, its exchanges and enabled memory context can go to the displayed provider. The JSON sharing export removes internal document-source previews. Text already quoted in visible messages remains part of the transcript.

Sparky normally connects to engines started by its installation. To deliberately reuse a separately managed llama.cpp or ComfyUI server on the configured loopback port, set `allow_external_llm` or `allow_external_comfy` to `true` in your configuration.

## 💻 Computer mode and security

Off by default. Pick a level with the **Computer** chip under the message box (it applies to that chat):

| Level | Reading (list, read, search, screenshot) | Changes (write, copy, move, trash) | Open apps · run commands |
|---|---|---|---|
| Off | — | — | — |
| Read only | ✅ | ❌ | ❌ |
| Read freely & ask before changes ★ | ✅ | 🙋 asks you | 🙋 asks you |
| Ask for everything | 🙋 asks you | 🙋 asks you | 🙋 asks you |
| Full access | ✅ | ✅ | ✅ |

Whatever the level: Windows, Program Files, ProgramData and Sparky's own folder can never be modified, a deletion always goes to the **Recycle Bin**, commands stop after 2 minutes, and the model is told that file contents are data, never instructions. "Always in this chat" only lasts until you change the level.

## 🔗 OpenAI-compatible API

Turn it on in *Workspace > API*, then point any OpenAI client at Sparky's active model:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:7860/v1", api_key="sk-sparky-...")  # key shown in Workspace > API
print(client.chat.completions.create(model="sparky", messages=[{"role": "user", "content": "Hello!"}]).choices[0].message.content)
```

## 🔌 MCP tools

*Workspace > Tools (MCP)* takes the same JSON as Claude Desktop, for example:

```json
{"mcpServers": {
  "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"]},
  "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
}}
```

Servers start with Sparky (the bundled Node.js provides `npx`); their tools are offered to the assistant in every chat.

## 🧪 Development

```powershell
runtime\venv\Scripts\python.exe -m pip install pytest ruff
runtime\venv\Scripts\python.exe -m ruff check app tests
runtime\venv\Scripts\python.exe -m pytest
```

The tests need no model, GPU or network: they run Sparky on throw-away folders, with scripted models and fake cloud providers (`tests/mocks.py`). GitHub Actions runs them on Windows (Python 3.10 and 3.13) and Linux, and checks that the PowerShell scripts parse in Windows PowerShell 5.1.

## 🇫🇷 En français

L'interface et l'installeur existent aussi en français : choisis *Français* au début de l'installation, ou change la langue dans *Paramètres*.

<p align="center"><img src="docs/french.png" alt="Sparky en français" width="760"></p>

## 🗂️ Project layout

```
app/
  server.py     web server + model management
  harness.py    agent: assistant <-> image tools loop, artifacts
  catalog.py    catalog, downloads, indicators
  syscheck.py   system scan and recommendations
  attach.py     attached files -> text (PDF, Office, code, audio/video transcription)
  localscan.py  finds the models already on the PC (LM Studio, Ollama, Hugging Face...)
  updater.py    version check and one-click update (git pull)
  cloud.py      optional cloud providers (OpenRouter, Claude, OpenAI-compatible...)
  checker.py    static syntax and local resource checks for generated projects
  stt.py        offline voice dictation (Whisper)
  tts.py        offline read-aloud (Piper voices through onnxruntime + espeak-ng)
  computer.py   computer mode: file / app / command tools, security levels, protected folders
  studio.py     Studio: media library, image / video / image-to-video jobs (LTX-2.5, Wan 2.2)
  rag.py        document collections: extraction, chunking, embeddings, search with citations
  web.py        web search and page reading (DuckDuckGo / Bing / SearXNG), private addresses blocked
  mcp.py        Model Context Protocol client (stdio and Streamable HTTP)
  workspace.py  assistants and prompt library
  scheduler.py  scheduled tasks
  openai_api.py OpenAI-compatible API (/v1)
  vendor/       Markdown libraries served locally (marked, DOMPurify, KaTeX, highlight.js, Mermaid)
  companion.py  Sparky Bot: persona, learning, reflection, journal (beta)
  memory.py     Sparky Bot memory: SQLite store + hybrid semantic retrieval
  index.html    interface (English / French)
install.ps1     step-by-step installer
VERSION         current version
requirements.txt  Sparky's own Python packages
tests/          regression tests (pytest), run by .github/workflows/ci.yml
start.ps1 / stop.ps1
docs/           screenshots and demos
```

## 📜 License

Code under the [MIT](LICENSE) license. ComfyUI (GPL-3.0) and llama.cpp (MIT) are downloaded separately during install.
