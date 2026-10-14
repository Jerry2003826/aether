.PHONY: check-pack test-reference
check-pack:
	python scripts/verify_freeze.py
test-reference:
	python -m unittest discover -s tests -p 'test_*.py' -v
