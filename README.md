# Reddit Tab Harvester

One-click Chrome extension that harvests your open Reddit tabs, categorizes content with AI, and builds a searchable knowledge base.

Uses the [Anthropic API](https://console.anthropic.com/) (Claude Haiku) for fast, lightweight analysis. Python stdlib only — no pip install needed.

## What it does

1. Click the extension icon — it finds all your Reddit tabs
2. Click **Harvest** — fetches content, runs AI analysis (~20 seconds for 10 tabs)
3. Opens a categorized digest with themes, one-liners, and full content
4. Every harvest feeds into a persistent **Knowledge Base** you can filter, sort, and prune

## How it works

```
Chrome Extension  ──POST urls──►  Local Python Server (localhost:7777)
                                    ├── Fetches Reddit .json API (parallel)
                                    ├── Sends to Anthropic API for analysis
                                    ├── Builds HTML digest
                                    └── Appends to Knowledge Base
```

- **Reddit .json API** — no auth needed, appends `.json` to any Reddit URL
- **Anthropic API** (Claude Haiku) — categorizes, summarizes, scores relevance
- **Knowledge Base** — all posts across sessions in one filterable page at `localhost:7777/knowledge`

## Requirements

- **macOS** (uses launchd for auto-start; server works on any OS but `install.sh` is macOS-specific)
- **Python 3.6+** (stdlib only, no pip install needed)
- **Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com/)
- **Google Chrome**

## Install

```bash
git clone https://github.com/sunlesshalo/reddit-tab-harvester.git
cd reddit-tab-harvester
```

Set your API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Run the installer:

```bash
bash install.sh
```

This starts the local server and prints instructions for loading the Chrome extension:

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder

## Usage

### Harvesting

1. Browse Reddit, open interesting tabs
2. Click the **Tab Harvester** icon in your toolbar
3. Click **Harvest** — watch the progress bar
4. Digest opens in a new tab with:
   - **Key Themes** — overarching patterns across your tabs
   - **Quick Scan** — posts grouped by category with one-liners
   - **Deep Read** — expandable full content + top comments
5. Optionally click **Close Harvested Tabs** to free up your browser

### Knowledge Base

Every harvest automatically adds posts to your knowledge base. Access it:

- Click **Knowledge Base** in the extension popup, or
- Visit `http://localhost:7777/knowledge`

Features:
- **Filter** by category: Ideas, Methods, Tools, Discussion, Reference
- **Sort** by relevance, date, or Reddit score
- **Dismiss** posts you no longer need (click the X)

### Categories

Each post is assigned to one of:

| Category | What it captures |
|----------|-----------------|
| **Ideas** | Business ideas, opportunities, market gaps |
| **Methods** | How-tos, frameworks, processes, strategies |
| **Tools** | Software, services, resources, templates |
| **Discussion** | Debates, opinions, trends, community insights |
| **Reference** | Data, benchmarks, case studies to bookmark |

## Files

```
reddit-tab-harvester/
├── server.py           # Python server (stdlib only) — all backend logic
├── prompt.txt          # Static analysis prompt
├── install.sh          # macOS setup: launchd + instructions
├── extension/
│   ├── manifest.json   # Chrome Manifest V3
│   ├── popup.html      # Extension popup UI
│   ├── popup.js        # Tab detection, harvest trigger, progress
│   └── icon.png        # Extension icon
└── data/               # Generated digests + knowledge base (gitignored)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server health check |
| `/harvest` | POST | Harvest URLs, return digest (JSON response) |
| `/harvest-stream` | POST | Harvest with SSE progress streaming |
| `/digest/<filename>` | GET | Serve a saved digest |
| `/digests` | GET | List recent digests |
| `/knowledge` | GET | Consolidated knowledge base page |
| `/knowledge/dismiss` | POST | Remove a post from knowledge base |

## How the analysis works

The server sends fetched Reddit content + a static prompt to the Anthropic API (Claude Haiku) and asks for **analysis only** — categories, one-liners, and relevance scores. The model does not echo back the full content, which keeps responses fast (~5 seconds for analysis).

The server then merges the analysis with the already-fetched content to build the final digest. This design keeps processing under 20 seconds for 10+ tabs.

## License

MIT
