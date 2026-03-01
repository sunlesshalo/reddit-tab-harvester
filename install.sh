#!/bin/bash
# Tab Harvester — One-time setup
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.tabharvester.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
PYTHON3="$(which python3)"

echo "=== Tab Harvester Setup ==="
echo ""

# 0. Check for API key
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "[!!] ANTHROPIC_API_KEY is not set."
    echo ""
    echo "Get one at: https://console.anthropic.com/"
    echo "Then run:   export ANTHROPIC_API_KEY=\"sk-ant-...\""
    echo "And re-run: bash install.sh"
    exit 1
fi

echo "[ok] ANTHROPIC_API_KEY is set"

# 1. Create launchd plist
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${SCRIPT_DIR}/server.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>ANTHROPIC_API_KEY</key>
        <string>${ANTHROPIC_API_KEY}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/data/server.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/data/server.log</string>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
</dict>
</plist>
EOF

echo "[ok] Created launchd plist at $PLIST_PATH"

# 2. Load the service
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "[ok] Loaded launchd service"

# 3. Wait and verify
sleep 2
if curl -s http://localhost:7777/health | grep -q '"ok"'; then
    echo "[ok] Server is running on http://localhost:7777"
else
    echo "[!!] Server may not have started. Check: ${SCRIPT_DIR}/data/server.log"
fi

echo ""
echo "=== Next: Load the Chrome Extension ==="
echo ""
echo "1. Open Chrome"
echo "2. Go to: chrome://extensions"
echo "3. Enable 'Developer mode' (top-right toggle)"
echo "4. Click 'Load unpacked'"
echo "5. Select: ${SCRIPT_DIR}/extension"
echo ""
echo "Done! Click the Tab Harvester icon to harvest Reddit tabs."
