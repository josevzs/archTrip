"""Dev entrypoint: python app.py  ->  http://localhost:8000"""
from archtrip import create_app

app = create_app()

if __name__ == "__main__":
    # threaded=True: the Flask dev server otherwise serialises requests and
    # misbehaves behind a keep-alive proxy. Docker uses gunicorn instead.
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
