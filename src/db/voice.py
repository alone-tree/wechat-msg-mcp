"""
语音转写（本地轻量方案）：sherpa-onnx + SenseVoiceSmall int8

- 模型一次性下载后纯本地推理，中文效果最好，5 秒语音亚秒级出字
- 转写结果按语音内容指纹（md5）永久缓存：同一条语音只转一次
- 模型文件放 decrypted/models/（gitignore 内），缓存放 decrypted/exports/
"""
import hashlib
import json
import os
import struct
import wave

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.path.join(PROJECT_ROOT, "decrypted", "models", "sensevoice")
CACHE_PATH = os.path.join(PROJECT_ROOT, "decrypted", "exports", "voice_transcribe_cache.json")

_recognizer = None  # 懒加载单例（进程内驻留）
_lock = None


def _get_lock():
    global _lock
    if _lock is None:
        import threading

        _lock = threading.Lock()
    return _lock


def _model_ready() -> bool:
    return os.path.isfile(os.path.join(MODEL_DIR, "model.int8.onnx")) and os.path.isfile(
        os.path.join(MODEL_DIR, "tokens.txt")
    )


def model_ready() -> bool:
    """语音识别模型是否就位。没就位时转写会直接失败，调用方应给出提示。"""
    return _model_ready()


def install_hint() -> str:
    """模型缺失时的安装提示，直接给 AI 看。"""
    return (
        "语音识别模型未安装，所以语音消息只能拿到音频、拿不到文字。"
        f"需要把 SenseVoiceSmall int8 的 model.int8.onnx 与 tokens.txt 放到 {MODEL_DIR}。"
        "模型来自 sherpa-onnx 官方模型库的 sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17。"
    )


def _load_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    import sherpa_onnx

    _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(MODEL_DIR, "model.int8.onnx"),
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        use_itn=True,
    )
    return _recognizer


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _read_wav_16k(wav_path: str):
    """读取 wav 并重采样到 16kHz float32（sherpa-onnx 要求）"""
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()
        raw = w.readframes(frames)
    duration = frames / rate if rate else 0.0

    if width == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 255.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    if rate != 16000 and len(samples) > 0:
        target_len = int(len(samples) * 16000 / rate)
        if target_len > 0:
            x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return samples, duration


def _transcribe_blob(blob: bytes, work_dir: str):
    """blob -> (text, duration)。失败抛异常。"""
    import pilk

    os.makedirs(work_dir, exist_ok=True)
    silk_path = os.path.join(work_dir, "tmp.silk")
    wav_path = os.path.join(work_dir, "tmp.wav")
    with open(silk_path, "wb") as f:
        f.write(blob)
    try:
        pilk.silk_to_wav(silk_path, wav_path, rate=24000)
    finally:
        try:
            os.remove(silk_path)
        except OSError:
            pass

    samples, duration = _read_wav_16k(wav_path)
    try:
        os.remove(wav_path)
    except OSError:
        pass

    recognizer = _load_recognizer()
    with _get_lock():
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
    text = (stream.result.text or "").strip()
    return text, duration


def transcribe_voice_blob(blob: bytes, work_dir: str):
    """带缓存的转写入口。返回 (text_or_None, duration_or_None)。"""
    if not blob:
        return None, None
    if not _model_ready():
        return None, None
    key = "md5:" + hashlib.md5(blob).hexdigest()
    cache = _load_cache()
    if key in cache:
        cached = cache[key]
        # 缓存格式：{"text": str, "duration": 秒}
        return cached.get("text"), cached.get("duration")

    try:
        text, duration = _transcribe_blob(blob, work_dir)
    except Exception:
        return None, None
    if not text:
        # 转出空文本（无声/杂音）也缓存，避免反复重试
        text = ""
    cache[key] = {"text": text, "duration": duration}
    _save_cache(cache)
    return text, duration
