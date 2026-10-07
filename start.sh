#!/usr/bin/env sh
set -eu
clonef_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$clonef_root"
if [ ! -x .venv/bin/python ]; then
    echo 'Сначала создайте окружение: python3 -m venv .venv' >&2
    echo 'Затем установите зависимости: .venv/bin/python -m pip install -r requirements.txt' >&2
    exit 1
fi
exec .venv/bin/python -m clonef "$@"

