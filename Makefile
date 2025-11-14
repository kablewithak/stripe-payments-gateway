.PHONY: help install test lint format docker-up docker-down migrate clean

help:
	@echo "Available commands:"
	@echo "  make install       - Install dependencies"
	@echo "  make test          - Run tests"
	@echo "  make lint          - Run linters"
	@echo "  make format        - Format code"
	@echo "  make docker-up     - Start Docker services"
	@echo "  make docker-down   - Stop Docker services"
	@echo "  make migrate       - Run database migrations"
	@echo "  make clean         - Clean temporary files"

install:
	pip install -r requirements.txt

test:
	pytest tests/ -v --cov=. --cov-report=html --cov-report=term

test-unit:
	pytest tests/ -v -m unit

test-integration:
	pytest tests/ -v -m integration

test-race:
	pytest tests/ -v -m race

test-load:
	locust -f tests/load_test.py --host=http://localhost:8000

lint:
	flake8 .
	mypy .
	pylint api core database integrations monitoring workers

format:
	black .
	isort .

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

migrate:
	alembic upgrade head

migrate-create:
	alembic revision --autogenerate -m "$(message)"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .coverage htmlcov

run:
	uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

run-worker-outbox:
	python -m workers.outbox_publisher

run-worker-reconciliation:
	python -m workers.reconciliation_worker
