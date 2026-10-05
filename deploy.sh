#!/bin/zsh
# Deploy GM Study Hall to a Linux VM: code + uv + Ollama (local notes model) + systemd.
# Usage: ./deploy.sh [host]   (or set GMSH_HOST to the server address)
set -euo pipefail
HOST=${1:-${GMSH_HOST:?set GMSH_HOST to the server address, or pass it as the first argument}}
USER_=Ubuntu
KEY=${GMSH_KEY:-~/.ssh/id_ed25519}
MODEL=${OLLAMA_MODEL:-hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M}
PORT=${PORT:-8080}
SSH=(ssh -i $KEY -o StrictHostKeyChecking=accept-new $USER_@$HOST)

cd "$(dirname "$0")"
rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  --exclude .venv --exclude data --exclude __pycache__ --exclude server.log --exclude node_modules --exclude tests --exclude share --exclude edge \
  ./ $USER_@$HOST:yt-notes/

"${SSH[@]}" MODEL=$MODEL PORT=$PORT 'bash -s' <<'REMOTE'
set -euo pipefail
dpkg -s ffmpeg libpango-1.0-0 libpangoft2-1.0-0 fonts-inter fonts-dejavu-core >/dev/null 2>&1 || { sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg libpango-1.0-0 libpangoft2-1.0-0 fonts-inter fonts-dejavu-core >/dev/null; }
command -v uv >/dev/null || [ -x ~/.local/bin/uv ] || curl -LsSf https://astral.sh/uv/install.sh | sh
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull "$MODEL"
cd ~/yt-notes && ~/.local/bin/uv sync -q

sudo tee /etc/systemd/system/yt-notes.service >/dev/null <<UNIT
[Unit]
Description=GM Study Hall
After=network-online.target ollama.service

[Service]
User=$USER
WorkingDirectory=$HOME/yt-notes
Environment=OLLAMA_MODEL=$MODEL
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$HOME/.local/bin/uv run uvicorn app:app --host 127.0.0.1 --port $PORT
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable yt-notes >/dev/null
sudo systemctl restart yt-notes
sudo ufw allow OpenSSH >/dev/null; sudo ufw delete allow $PORT/tcp >/dev/null 2>&1 || true; sudo ufw default deny incoming >/dev/null; sudo ufw --force enable >/dev/null
echo "deployed (localhost:$PORT, reachable via the Cloudflare tunnel only)"
REMOTE
