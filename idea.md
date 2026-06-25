# Speech Text Transformer - Product Idea and Build Plan

## 1) Muc tieu

Xay dung mot nen tang hop nhat:

- Speech to Text (STT) co the chon engine: Vosk hoac Whisper.
- Text to Speech (TTS) su dung OmniVoice tai duong dan local: /Users/lugon/code/OmniVoice.
- Ho tro streaming theo thoi gian thuc, SSE, va REST API cho client.
- Co the nhan audio/text theo luong (stream), va tra ket qua tung phan (partial) + ket qua cuoi (final).

## 2) Yeu cau chuc nang

### 2.1 STT

- Batch transcribe: upload audio, tra transcript.
- Realtime transcribe: client stream audio chunk, server tra partial/final transcript.
- Engine abstraction:
	- Vosk: uu tien do tre thap, CPU-friendly.
	- Whisper (de xuat faster-whisper): uu tien chat luong.

### 2.2 TTS

- Batch synthesize: nhan text, tra file audio.
- Pseudo-streaming TTS:
	- Tach text theo cau/phrase.
	- Generate theo chunk bang OmniVoice.
	- Day audio_chunk qua SSE de client phat progressive playback.

### 2.3 Event stream

- Su dung SSE cho event progress, partial/final result, error.
- Event schema thong nhat cho STT/TTS.
- Co job_id/session_id de client subscribe lai neu mat ket noi.

## 3) Rang buoc ky thuat va nhan dinh

- OmniVoice hien tai generate theo doan audio, chua co native token-level audio streaming.
- Vi vay TTS realtime duoc trien khai theo huong pseudo-streaming (chunk-based generation).
- STT streaming nen uu tien WebSocket cho uplink audio lien tuc.
- SSE la kenh push downlink don gian, phu hop cho transcript/progress/audio chunk metadata.

## 4) Kien truc tong the

### 4.1 Thanh phan

- API Gateway (FastAPI): auth, routing, session/job lifecycle.
- STT Service:
	- Provider interface.
	- Vosk provider.
	- Whisper provider.
	- Streaming pipeline (buffer + VAD + decoder).
- TTS Service:
	- OmniVoice adapter.
	- Text segmenter.
	- Chunk scheduler.
- Event Bus:
	- Ban dau in-memory.
	- Nang cap Redis Pub/Sub khi can scale.
- Storage:
	- Redis cho session state.
	- Object storage (S3-compatible) cho audio artifact.

### 4.2 Luong du lieu

#### STT streaming

1. Client mo WebSocket den /v1/stt/stream.
2. Client gui audio chunk PCM16/16k/mono.
3. Server xu ly theo frame, tra partial transcript dinh ky.
4. Khi ngat cau/noi xong, server tra final transcript.
5. Event dong bo qua SSE cho UI (neu can).

#### TTS pseudo-streaming

1. Client POST text den /v1/tts/stream -> nhan job_id.
2. Client subscribe SSE /v1/events/{job_id}.
3. Server tach text, generate tung chunk bang OmniVoice.
4. Moi chunk phat event audio_chunk (kem URL hoac base64).
5. Ket thuc phat event done.

## 5) API de xuat (v1)

### 5.1 STT

- POST /v1/stt/transcribe
	- Input: file audio + engine (vosk|whisper) + language(optional)
	- Output: transcript, confidence(optional), timing(optional)

- WS /v1/stt/stream
	- Client -> server: audio_chunk, flush, end
	- Server -> client: partial, final, vad_state, done, error

### 5.2 TTS

- POST /v1/tts/synthesize
	- Input: text, voice options (ref_audio/ref_text/instruct), speed, language
	- Output: audio_url hoac binary stream

- POST /v1/tts/stream
	- Input: text + options
	- Output: job_id

- GET /v1/events/{job_id} (SSE)
	- Event types: queued, progress, audio_chunk, final_audio, done, error

## 6) Cau truc du an de xuat

```
speech-text-transformer/
	apps/
		api-gateway/
		worker/
	services/
		stt/
			providers/
				vosk_provider.py
				whisper_provider.py
			streaming/
		tts/
			providers/
				omnivoice_provider.py
			streaming/
	shared/
		schemas/
		events/
		audio/
		config/
	tests/
		unit/
		integration/
		e2e/
	infra/
		docker/
		compose/
	docs/
	pyproject.toml
	README.md
```

## 7) Ke hoach trien khai theo phase

### Phase 0 - Skeleton

- Tao FastAPI app + healthcheck + settings + logging.
- Dinh nghia schema request/response/event.
- Chuan hoa audio format policy.

### Phase 1 - STT batch

- Tao STT provider interface.
- Tich hop Vosk + Whisper cho transcribe batch.
- Them test co ban cho quality va latency.

### Phase 2 - STT streaming

- Hoan thien WS audio ingest + partial/final events.
- Them VAD va silence endpointing.
- Mirror event qua SSE de UI de tich hop.

### Phase 3 - TTS OmniVoice batch

- Tich hop OmniVoice adapter local path.
- Ho tro mode clone/instruct/auto voice.
- Tra audio artifact + metadata.

### Phase 4 - TTS pseudo-streaming

- Text segmentation + chunk scheduler.
- SSE audio_chunk/progress.
- Toi uu first-byte time.

### Phase 5 - Reliability + Observability

- Queue, retry, timeout, circuit breaker co ban.
- Metrics: first_chunk_latency, final_latency, real_time_factor, error_rate.
- Structured logging + tracing.

### Phase 6 - Hardening

- Integration tests end-to-end.
- Docker compose cho local dev.
- Tai lieu API + runbook.

## 8) Rui ro va giai phap

- TTS cham voi text dai:
	- Giai phap: tach text + pre-generate chunk ke tiep.
- Tai nguyen GPU han che:
	- Giai phap: worker pool rieng cho STT Whisper va TTS OmniVoice.
- SSE payload lon:
	- Giai phap: gui chunk URL thay vi base64 khi can.
- Khac sample rate:
	- Giai phap: ap dat audio contract toan he thong + resample tai edge.

## 9) MVP scope de bat dau ngay

- STT:
	- Batch transcribe voi Vosk va Whisper.
	- Streaming qua WS voi partial/final.
- TTS:
	- Batch synthesize voi OmniVoice.
	- Stream progress + audio_chunk qua SSE.
- Client co the:
	- Stream audio len de nhan transcript.
	- Gui text de nhan audio dan theo chunk.

## 10) Definition of Done cho MVP

- Co API docs va vi du client.
- Co test integration cho 2 luong:
	- Audio stream -> transcript.
	- Text -> SSE chunk -> audio playback.
- Chay local bang 1 lenh compose.
- Co metric latency co ban va log truy vet theo session/job.
