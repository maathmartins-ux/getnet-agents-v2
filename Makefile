# Atalhos para Linux/CI. No Windows, rode os comandos python diretamente.
.PHONY: check check-docker eval readme up

check:
	python scripts/check.py

check-docker:
	python scripts/check.py --docker

eval:
	python -m evals.run_evals

readme: eval
	python scripts/fill_readme.py

up:
	docker compose up -d --build
