lint:
	pylint --rcfile .pylintrc --load-plugins pylint_pydantic --extension-pkg-whitelist='pydantic' smartfeed
	mypy --config-file setup.cfg smartfeed
	black --check --config black.toml smartfeed

format:
	black --verbose --config black.toml smartfeed tests
	isort --sp .isort.cfg smartfeed tests
