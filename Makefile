lint:
	ruff check smartfeed tests
	pyright smartfeed tests
	black --check smartfeed tests

format:
	black smartfeed tests
	isort smartfeed tests

test:
	pytest
