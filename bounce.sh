#!/bin/zsh
set -e

GUI="gui/$(id -u)"

echo "Restarting Option Dashboard..."
launchctl kickstart -k "${GUI}/com.options.dashboard"

echo "Restarting Cloudflare Tunnel..."
launchctl kickstart -k "${GUI}/com.otj.tunnel"

sleep 4

if curl -s -o /dev/null -w "%{http_code}" http://localhost:5051/ | grep -q "200"; then
    echo "Dashboard is up at http://localhost:5051"
else
    echo "WARNING: dashboard not responding on :5051"
    echo "  tail -30 ${HOME}/logs/options-dashboard.log"
fi
