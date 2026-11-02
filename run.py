"""Sobe o painel local. Uso: python run.py"""

import uvicorn

if __name__ == "__main__":
    print("Painel em http://127.0.0.1:8000  (Ctrl+C para parar)")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
