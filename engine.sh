#!/bin/bash
set -euo pipefail
pipewire &
wireplumber &
pipewire-pulse &
wait -n
exit 1
