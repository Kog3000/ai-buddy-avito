.PHONY: install run test e2e coverage docker

install:
	pip install -r requirements-dev.txt

run:
	python wsgi.py

test:
	python -m pytest tests --cov=app --cov-report=term-missing --cov-report=html:coverage/html --cov-report=xml:coverage/coverage.xml

e2e:
	python -m pytest e2e -p no:logging

coverage: test e2e
	@cat coverage/js-coverage.txt

docker:
	docker compose up --build
