#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y libbrotli1 minisign
directory=$(mktemp -d "${RUNNER_TEMP:-/tmp}/kinora-tools.XXXXXX")
archive="$directory/7zip.tar.xz"
curl --fail --silent --show-error --location --retry 2 --max-time 60 \
  --proto '=https' --proto-redir '=https' \
  'https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz' \
  --output "$archive"
printf '%s  %s\n' '2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e' "$archive" \
  | sha256sum --check
tar -xJf "$archive" -C "$directory" 7zz
printf '%s\n' "$directory" >> "$GITHUB_PATH"
