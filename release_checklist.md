# Release checklist

- run tests (`py.test`)
- bump version in `__init__.py`
- update `CHANGES.txt`
- confirm dependencies/classifiers in `pyproject.toml` are current
- commit changes on dev branch, create pr, merge pr to main
- swich to main branch locally, pull latest, create tag & push:
		
		git checkout main
		git pull
		git tag -a <version> -m <version> && git push

Note that releases are automatically published (to pypi & github) on tag via `.github/workflows/release.yml`