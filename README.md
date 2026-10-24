# Brasimundo ingestion v0.1

1. Create a free GitHub repo (e.g. `brasimundo-data`).
2. Upload `ingest.py` to the repo root and `ingest.yml` to `.github/workflows/ingest.yml`.
3. Repo Settings > Actions > General > Workflow permissions > "Read and write".
4. Actions tab > "ingest" > Run workflow.
5. Check `data/status.json`: `"live_fetch": "success"` is your proof of a live collection.

If a source fails, `status.json` shows the error; paste it to Claude and it gets fixed.
