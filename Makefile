.PHONY: install backend frontend test-backend build-frontend

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test-backend:
	cd backend && .venv/bin/python -m unittest discover -s tests -v

build-frontend:
	cd frontend && npm run build
