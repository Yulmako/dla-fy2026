# Publishing the web app (Streamlit Community Cloud)

The app (`app_web/streamlit_app.py`) shows the same FY2026 DLA dashboard plus a search over
**every** FY2026 DLA contract action. The data is one SQLite file built from your local
database; the code goes to GitHub, the data file goes to a GitHub *release* (repositories
reject files over 100 MB; release assets may be up to 2 GB).

Everything published is public USAspending.gov data. No SAM.gov data, no API keys, no
credentials. Do not commit `data/` (it is in `.gitignore`).

## 1. Build the data file (on your computer)

```
python scripts/export_dashboard_snapshot.py --app-db
```

This writes, besides the usual `dashboard_snapshot.zip`:

* `data/processed/app_data.sqlite` – the web-app database (orders + full-text index + all tables)
* `data/processed/app_data.sqlite.zip` – the same, compressed, for upload

If the zip is over 2 GB, rebuild with shorter descriptions: `--app-desc-chars 80`.

Try it locally first (optional):

```
pip install -r app_web/requirements.txt
streamlit run app_web/streamlit_app.py
```

(Locally the app reads `data/processed/app_data.sqlite` directly.)

## 2. Put the code on GitHub

1. Create a new **public** repository on github.com (e.g. `dla-fy2026-intel`).
2. Upload the project folder contents (everything from the zip; `data/` is excluded).
   With git:
   ```
   git init && git add . && git commit -m "FY2026 DLA competitor intelligence"
   git branch -M main
   git remote add origin https://github.com/<you>/dla-fy2026-intel.git
   git push -u origin main
   ```

## 3. Upload the data file as a release asset

1. In the repository: **Releases → Create a new release**, tag `data-fy2026-v1`.
2. Drag `app_data.sqlite.zip` into the assets box and publish.
3. Right-click the asset → copy link. It looks like
   `https://github.com/<you>/dla-fy2026-intel/releases/download/data-fy2026-v1/app_data.sqlite.zip`

## 4. Create the app

1. Go to https://share.streamlit.io, sign in with GitHub, **Create app → Deploy a public app from GitHub**.
2. Repository: your repo, branch `main`, main file path: `app_web/streamlit_app.py`.
3. **Advanced settings → Secrets**, paste:
   ```
   APP_DATA_URL = "https://github.com/<you>/dla-fy2026-intel/releases/download/data-fy2026-v1/app_data.sqlite.zip"
   ```
4. Deploy. The first start downloads and unpacks the data (a few minutes); later visits are fast
   until the app sleeps.

## Updating the data

Re-run the pipeline, then `export_dashboard_snapshot.py --app-db`, create a new release
(e.g. `data-fy2026-v2`), update `APP_DATA_URL` in the app's secrets and reboot the app
(**Manage app → Reboot**).

## Notes

* Free Community Cloud apps have about 1 GB of memory. The app keeps the data on disk (SQLite)
  and only loads what each page shows, so the full year fits.
* Obligations are federal contract obligations, not company revenue or profit.
* Anyone with the link can see the app. All content is public USAspending.gov data.
