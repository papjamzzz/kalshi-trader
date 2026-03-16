.PHONY: setup run clean push

setup:
	python3 -m venv venv
	./venv/bin/pip install -q --upgrade pip
	./venv/bin/pip install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env; echo "  ✅ .env created — add your keys"; fi

run:
	./venv/bin/python app.py

clean:
	find . -name '__pycache__' -exec rm -rf {} + 2>/dev/null; true
	find . -name '*.pyc' -delete 2>/dev/null; true

push:
	git add -A
	git commit -m "update kk-trader"
	git push
