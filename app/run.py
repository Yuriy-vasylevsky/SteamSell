"""Container entrypoint; Railway supplies the listening port through PORT."""

import os

import uvicorn


def main():
    port = int(os.environ.get("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
