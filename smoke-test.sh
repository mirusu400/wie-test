#!/usr/bin/env bash
# Smoke test that mirrors .github/workflows/daily.yml inside ubuntu:24.04.
# Skips corpus download — uses a single dummy .jad to exercise the pipeline.
set -euo pipefail

echo "::: apt deps"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl git build-essential pkg-config \
  python3 python3-pip python3-venv \
  xvfb \
  libx11-6 libxcursor1 libxi6 libxrandr2 \
  libxkbcommon-x11-0 \
  libxcb1 libxcb-render0 libxcb-shape0 libxcb-xfixes0 \
  libwayland-client0 \
  libgl1 libegl1 libgl1-mesa-dri \
  libasound2-dev libudev-dev \
  >/dev/null

echo "::: rust"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y --default-toolchain stable --profile minimal >/dev/null
. "$HOME/.cargo/env"

echo "::: clone wie"
git clone --depth 1 https://github.com/dlunch/wie.git /opt/wie
echo "  wie HEAD: $(git -C /opt/wie rev-parse HEAD)"

echo "::: cargo build --release --bin wie_cli (slow)"
cargo build --release --manifest-path /opt/wie/Cargo.toml --bin wie_cli

WIE_BIN=/opt/wie/target/release/wie_cli
test -x "$WIE_BIN" || { echo "wie_cli not built at $WIE_BIN" >&2; exit 1; }

echo "::: stage workdir"
mkdir -p /work && cd /work
cp /repo/run_compat.py /repo/requirements.txt .
python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt

echo "::: dummy corpus"
mkdir -p corpus
cat > corpus/smoke.jad <<'EOF'
MIDlet-Name: smoke
MIDlet-Version: 1.0
MIDlet-Vendor: smoke
MIDlet-Jar-URL: smoke.jar
MIDlet-Jar-Size: 0
EOF

echo "::: run_compat.py test --limit 1 --timeout 5"
xvfb-run -a --server-args='-screen 0 1024x768x24' \
  python run_compat.py test --wie "$WIE_BIN" --timeout 5 --limit 1 --jobs 1

echo "::: run_compat.py report"
python run_compat.py report

echo
echo "===== report.md ====="
cat report.md
echo "===== results/*.json ====="
for f in results/*.json; do echo "--- $f ---"; cat "$f"; done

echo
echo "::: SMOKE OK"
