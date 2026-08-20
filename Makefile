.PHONY: install install-all test doctor docker-build clean

install:
	pip install -e .
	playwright install chromium
	@echo "PAC-CLI installed. Run: pac doctor --compact"

install-all:
	pip install -e ".[all]"
	playwright install chromium
	camoufox fetch
	@echo "PAC-CLI full stealth suite installed. Run: pac doctor --compact"

test:
	pytest -q

doctor:
	pac doctor --compact

docker-build:
	docker build -t logicrw/pac-cli .

clean:
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -type d -name "__pycache__" -exec rm -rf {} +
