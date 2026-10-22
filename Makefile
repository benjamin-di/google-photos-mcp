.PHONY: bootstrap check lock
bootstrap:
	./scripts/bootstrap.sh
check:
	./scripts/test.sh
# After editing dependencies in pyproject.toml: refresh uv.lock and the images' hash locks.
lock:
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header --quiet -o requirements-runtime.lock
	uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o requirements-login.lock
