"""Transcrição local opcional com faster-whisper.

Instalar requirements-ai.txt na VPS ou worker. Nenhum áudio sai do servidor.
"""
from pathlib import Path
import os
import tempfile
import subprocess

def to_srt_time(seconds):
    ms=max(0,int(round(float(seconds)*1000)))
    h,ms=divmod(ms,3600000)
    m,ms=divmod(ms,60000)
    s,ms=divmod(ms,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def transcribe(ffmpeg, path, srt_path, language="pt", model_name=None):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Transcrição local não instalada. Instale 'pip install -r requirements-ai.txt' no worker/VPS.") from exc
    model_name=model_name or os.environ.get("WHISPER_MODEL","small")
    if model_name not in {"tiny", "base", "small", "medium"}:
        raise ValueError("Modelo de transcrição inválido.")
    with tempfile.TemporaryDirectory(prefix="editor-audio-") as tmp:
        wav=Path(tmp)/"speech.wav"
        p=subprocess.run([ffmpeg,"-hide_banner","-y","-nostdin","-loglevel","error",
                          "-i",str(path),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)],
                         capture_output=True,text=True,timeout=900)
        if p.returncode or not wav.is_file():
            raise RuntimeError("Não foi possível extrair o áudio do vídeo.")
        model=WhisperModel(model_name,device="cpu",compute_type="int8")
        segments,info=model.transcribe(str(wav),language=language,beam_size=4,
                                       vad_filter=True,word_timestamps=True)
        lines=[]
        index=0
        for seg in segments:
            words=seg.words or []
            if not words:
                index+=1
                lines.extend([str(index),f"{to_srt_time(seg.start)} --> {to_srt_time(seg.end)}",seg.text.strip(),""])
            else:
                # Legendas curtas, até ~5 palavras por cartão.
                for start in range(0,len(words),5):
                    group=words[start:start+5]
                    index+=1
                    caption=" ".join(w.word.strip() for w in group).strip()
                    lines.extend([str(index),f"{to_srt_time(group[0].start)} --> {to_srt_time(group[-1].end)}",caption,""])
        Path(srt_path).write_text("\n".join(lines),encoding="utf-8")
        return {"segments":index,"language":info.language}
