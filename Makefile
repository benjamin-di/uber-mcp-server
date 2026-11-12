.PHONY: bootstrap check check-connections lock
bootstrap:
	uv sync --locked
check:
	./scripts/test.sh
check-connections:
	bash scripts/check-connections.sh
# After editing dependencies in pyproject.toml: refresh uv.lock and the images' hash locks.
lock:
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header --quiet -o requirements-runtime.lock
	uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o requirements-login.lock
