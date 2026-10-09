from app import create_app

app = create_app()

if __name__ == "__main__":
    # use_reloader=False: the reloader would start the app twice (and recover jobs twice).
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
