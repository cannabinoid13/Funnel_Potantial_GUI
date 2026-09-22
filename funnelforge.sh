#!/usr/bin/env bash
# FunnelForge launcher: activates the conda environment and starts the GUI.
#
#   ./funnelforge.sh                                    # empty session
#   ./funnelforge.sh /path/to/job.pot                   # import a job set
#   ./funnelforge.sh /path/to/job.cms                   # any member works
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="${FUNNELFORGE_ENV:-cugraph_env}"

for base in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" \
            "/opt/conda" "${CONDA_PREFIX:-}/.."; do
  if [[ -f "$base/etc/profile.d/conda.sh" ]]; then
    # shellcheck disable=SC1091
    source "$base/etc/profile.d/conda.sh"
    break
  fi
done

if command -v conda >/dev/null 2>&1; then
  if [[ "${CONDA_DEFAULT_ENV:-}" != "$ENV_NAME" ]]; then
    conda activate "$ENV_NAME"
  fi
else
  echo "conda not found; using the python already on PATH" >&2
fi

missing=()
for mod in numpy scipy vtk PyQt5 matplotlib; do
  python - "$mod" <<'PY' || missing+=("$mod")
import importlib, sys
importlib.import_module(sys.argv[1])
PY
done
if (( ${#missing[@]} )); then
  echo "Missing python packages: ${missing[*]}" >&2
  echo "Install them with:  ./install.sh" >&2
  exit 1
fi

cd "$HERE"
exec python -m funnelforge "$@"
