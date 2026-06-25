from app.services.stt.service import stt_service

if __name__ == "__main__":
    for engine in stt_service.list_engines():
        print(engine)
