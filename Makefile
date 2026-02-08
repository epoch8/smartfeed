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

.PHONY: test_async_chart charting

# Runs only the async loop block + Chrome trace test.
# Writes trace.json next to this Makefile (project root).
test_async_chart:
	rm -f ./trace.json
	SMARTFEED_CHROME_TRACE=./trace.json pytest -q tests/test_async_loop_blocks_trace.py
	@echo "\nWrote trace: $(CURDIR)/trace.json"
	@echo "Open Chrome -> chrome://tracing -> Load -> select trace.json"

# Convenience target: generate the trace + try to open chrome://tracing.
charting: test_async_chart
	-@open -a "Google Chrome" "chrome://tracing" 2>/dev/null || true
