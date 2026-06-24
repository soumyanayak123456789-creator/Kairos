"""Clutch — minimal Cloud Run skeleton (build step 1).

This intentionally contains NO app logic yet: just a single health endpoint to
prove the Cloud Run deploy path before any OAuth / Firestore / Gemini work.
"""

from fastapi import FastAPI

app = FastAPI(title="Clutch", description="The Last-Minute Life Saver")


@app.get("/")
def root():
    return {"status": "ok", "service": "clutch"}
