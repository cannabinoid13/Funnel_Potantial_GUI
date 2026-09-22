#!/usr/bin/env bash
# Installs the python packages FunnelForge needs into the conda environment.
set -euo pipefail

ENV_NAME="${FUNNELFORGE_ENV:-cugraph_env}"
for base in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "/opt/conda"; do
  if [[ -f "$base/etc/profile.d/conda.sh" ]]; then
    # shellcheck disable=SC1091
    source "$base/etc/profile.d/conda.sh"
    break
  fi
done
conda activate "$ENV_NAME"

echo "environment : $CONDA_DEFAULT_ENV"
echo "python      : $(python -c 'import sys; print(sys.version.split()[0])')"

need=()
while read -r mod pkg; do
  [[ -z "$mod" ]] && continue
  if ! python -c "import $mod" >/dev/null 2>&1; then
    need+=("$pkg")
  else
    printf '  present  %-12s %s\n' "$mod" \
      "$(python -c "import $mod,sys; print(getattr($mod,'__version__','?'))" 2>/dev/null)"
  fi
done <<'LIST'
numpy numpy>=1.22
scipy scipy>=1.8
vtk vtk>=9.1
PyQt5 PyQt5>=5.15
matplotlib matplotlib>=3.5
LIST

if (( ${#need[@]} )); then
  echo "installing: ${need[*]}"
  python -m pip install --upgrade "${need[@]}"
else
  echo "all dependencies are already present"
fi

echo
echo "running the self-tests"
python "$(dirname "${BASH_SOURCE[0]}")/tests/test_funnelforge.py" || true
