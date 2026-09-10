#!/usr/bin/env bash
# In-sandbox capability probe used by Spike C (direct `codex sandbox` and agent-run).
# Prints only exit codes, error text, and env var NAMES; never prints values.
echo "-- uid: $(id -u)"
echo "-- curl https://api.github.com/:"; curl -sS -m 5 -o /dev/null -w "http=%{http_code}\n" https://api.github.com/; echo "curl exit=$?"
echo "-- gh api user:"; gh api user 2>&1 | head -2; echo "gh exit=${PIPESTATUS[0]}"
echo "-- getent hosts github.com:"; getent hosts github.com; echo "getent exit=$?"
echo "-- python socket connect 140.82.112.3:443:"; python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('140.82.112.3',443)); print('CONNECTED')" 2>&1 | tail -1
echo "-- ip link:"; ip -o link 2>&1 | head -3
echo "-- write repo file:"; touch "$PWD/.sbx_write_test" 2>&1; echo "touch exit=$?"; rm -f "$PWD/.sbx_write_test"
echo "-- write ~/.codex/sbx_test:"; touch "$HOME/.codex/sbx_test" 2>&1; echo "touch exit=$?"; rm -f "$HOME/.codex/sbx_test"
echo "-- read ~/.config/gh/hosts.yml:"; test -r "$HOME/.config/gh/hosts.yml" && echo READABLE || echo "NOT READABLE"
echo "-- env names with TOKEN/KEY:"; env | cut -d= -f1 | grep -i -E "token|key|secret" | sort | tr '\n' ' '; echo
