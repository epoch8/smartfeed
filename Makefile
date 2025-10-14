lint:
	mypy smartfeed
	black --check smartfeed

format:
	black --verbose smartfeed tests
	isort smartfeed tests

test:
	pytest -s -vv -k "not test_merger_view_session"

test_cache:
	pytest -s -vv -k "test_merger_view_session"
