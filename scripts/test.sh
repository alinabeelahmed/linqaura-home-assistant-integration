#!/bin/sh
# Runs the tests inside the Home Assistant container (Python 3.14, HA's own dependencies), so nothing big is
# downloaded. pytest-homeassistant-custom-component lags HA by a release; the two patches below make 0.13.375
# (built for HA 2026.10) work on 2026.11. Usage from the repository root:
#   MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/w" -w /w --entrypoint sh ghcr.io/home-assistant/home-assistant:stable scripts/test.sh
set -e
SITE=$(python -c "import site; print(site.getsitepackages()[0])")
pip install -q --root-user-action=ignore --no-deps pytest-homeassistant-custom-component==0.13.375
pip install -q --root-user-action=ignore pytest==9.0.3 pytest-asyncio==1.4.0 pytest-aiohttp==1.1.1 \
  pytest-freezer==0.4.9 freezegun==1.5.5 pytest-socket==0.8.1 pytest-timeout==2.4.0 pytest-unordered==0.8.0 \
  pytest-cov==7.1.0 respx==0.23.1 requests-mock==1.12.1 syrupy==6.1.1 pytest-picked pytest-xdist mock-open 2>/dev/null
# HA 2026.11 aliases httpx itself and must be imported before respx is
echo "import homeassistant  # noqa: F401" > "$SITE/lq_early.py"
# HA 2026.11 moved _clear_logger_overwrites to logger.helpers
python - "$SITE/pytest_homeassistant_custom_component/common.py" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read()
t = t.replace("    SERVICE_SET_LEVEL,\n    _clear_logger_overwrites,\n)", "    SERVICE_SET_LEVEL,\n)\nfrom homeassistant.components.logger.helpers import _clear_logger_overwrites")
open(p, "w").write(t)
PY
python -m pytest -p lq_early -p no:cacheprovider --cov=custom_components.linqaura --cov-report=term-missing "$@"
