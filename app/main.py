from fastapi import FastAPI

app = FastAPI(title="auto-split")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
