import json
import base64
import secrets
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import imageio_ffmpeg
from render_engine import parse_scenes, render
from smart_edit import automatic_plan
from captions import transcribe

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Editor de Vídeo Automático")
@app.middleware("http")
async def optional_password_gate(request, call_next):
    password = os.getenv("EDITOR_PASSWORD", "")
    if password:
        auth = request.headers.get("Authorization", "")
        allowed = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
                username, supplied = decoded.split(":", 1)
                allowed = (secrets.compare_digest(username, os.getenv("EDITOR_USERNAME", "admin"))
                           and secrets.compare_digest(supplied, password))
            except (ValueError, UnicodeDecodeError):
                pass
        if not allowed:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Autenticação necessária para acessar o editor.", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="Editor de Video"'})
    return await call_next(request)


DATA = Path(os.getenv("EDITOR_DATA_DIR", "/data"))
try:
    DATA.mkdir(parents=True, exist_ok=True)
    probe_path = DATA / ".write-check"
    probe_path.write_text("ok")
    probe_path.unlink()
except (OSError, PermissionError):
    DATA = Path("/tmp/editor-video-data")
    DATA.mkdir(parents=True, exist_ok=True)
MAX_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(4 * 1024 * 1024 * 1024)))
FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
jobs = {}
lock = threading.Lock()
RENDER_CONCURRENCY = max(1, min(4, int(os.getenv("MAX_PARALLEL_RENDERS", "1"))))
RENDER_SLOTS = threading.BoundedSemaphore(RENDER_CONCURRENCY)
KEEP_DAYS = max(1, min(365, int(os.getenv("EDITOR_KEEP_DAYS", "7"))))


def prune_old_files():
    cutoff = time.time() - KEEP_DAYS * 86400
    with lock:
        active = {k for k,v in jobs.items()
                  if v.get("state") in {"na_fila","analisando","processando"}}
    try:
        for folder in DATA.iterdir():
            if not folder.is_dir() or folder.name in active:
                continue
            if folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass



def set_job(job_id, **changes):
    with lock:
        jobs[job_id].update(changes)


def run_probe(path):
    # FFmpeg é instalado pela dependência imageio-ffmpeg, mesmo em hospedagens
    # Python que não disponibilizam o executável ffprobe no PATH.
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path), "-t", "0.01", "-f", "null", "-"],
        text=True, capture_output=True, timeout=90
    )
    info = result.stderr
    if not re.search(r"Stream #\d+:\d+.*Video:", info):
        raise ValueError("O arquivo enviado não contém um fluxo de vídeo válido.")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)
    if not duration_match:
        raise ValueError("Não foi possível identificar a duração do vídeo.")
    h, m, sec = duration_match.groups()
    duration = int(h) * 3600 + int(m) * 60 + float(sec)
    if duration <= 0:
        raise ValueError("A duração do vídeo é inválida.")
    has_audio = bool(re.search(r"Stream #\d+:\d+.*Audio:", info))
    return duration, has_audio


def detect_kept_segments(path, duration):
    """Detect long silences and preserve surrounding speech; return segments or None."""
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path), "-af",
         "silencedetect=noise=-36dB:d=1.2", "-vn", "-f", "null", "-"],
        text=True, capture_output=True, timeout=max(120, int(duration * 4))
    )
    if p.returncode != 0:
        return None
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", p.stderr)]
    silences = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        # keep brief breathing space around cuts
        a, b = max(0, start + 0.18), min(duration, end - 0.18)
        if b > a:
            silences.append((a, b))
    if not silences:
        return None
    segments = []
    pos = 0.0
    for a, b in silences:
        if a - pos >= 0.35:
            segments.append((pos, a))
        pos = max(pos, b)
    if duration - pos >= 0.35:
        segments.append((pos, duration))
    if not segments or len(segments) > 120:
        return None
    if sum(b - a for a, b in segments) < 1:
        return None
    return segments


def make_filters(preset, has_audio, intensity=65):
    if preset == "dinamico":
        v = ("hqdn3d=1.5:1.5:5:5,eq=contrast=1.07:saturation=1.17:brightness=0.008,"
             "unsharp=5:5:0.65:3:3:0.25,"
             "crop=iw*0.96:ih*0.96:(iw-ow)*(0.5+0.24*sin(n/65)):(ih-oh)*(0.5+0.18*cos(n/83))")
    elif preset == "suave":
        v = "hqdn3d=2:2:6:6,eq=contrast=1.035:saturation=1.06,unsharp=5:5:0.32:3:3:0.1"
    else:
        v = "hqdn3d=1.5:1.5:5:5,eq=contrast=1.055:saturation=1.10,unsharp=5:5:0.48:3:3:0.15"
    # Intensidade visível: 0 a 100, usando parâmetros FFmpeg limitados
    strength = max(0, min(100, int(intensity))) / 100
    if strength == 0:
        v = "null"
    else:
        v = (
            f"hqdn3d=1.5:1.5:5:5,eq=contrast={1+0.19*strength:.3f}:"
            f"saturation={1+0.38*strength:.3f}:brightness={0.018*strength:.3f},"
            f"unsharp=5:5:{0.98*strength:.3f}:3:3:{0.32*strength:.3f}"
        ) + (",crop=iw*0.95:ih*0.95:(iw-ow)*(0.5+0.24*sin(n/65)):(ih-oh)*(0.5+0.18*cos(n/83))" if preset=="dinamico" else "")
    # ensure compatible 4:2:0 output dimensions
    v += ",scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,format=yuv420p"
    a = "highpass=f=75,lowpass=f=15000,afftdn=nf=-24,acompressor=threshold=-20dB:ratio=2.4:attack=15:release=220,loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.94"
    return v, a if has_audio else None


def process(*args):
    job_id = args[0]
    set_job(job_id, message="Aguardando vaga para renderização", state="na_fila")
    with RENDER_SLOTS:
        return _process_unlocked(*args)


def _process_unlocked(job_id, source, music, cover, preset, cuts, start_at, end_at, intensity,
            scenes_json, audio_mode, voice_gain, music_gain, output_format, subtitle_file):
    try:
        set_job(job_id, state="analisando", message="Analisando duração, cenas e faixas de áudio", progress=2)
        duration, has_audio = run_probe(source)
        start_at = max(0.0, min(float(start_at), duration - 0.1))
        end_at = min(duration, float(end_at)) if float(end_at) > 0 else duration
        if end_at - start_at < 0.3:
            raise ValueError("O intervalo selecionado precisa ter pelo menos 0,3 segundo.")
        mapped_style = {"equilibrado": "natural", "dinamico": "vibrante", "suave": "natural"}.get(preset, "natural")
        scenes = parse_scenes(scenes_json, duration, start_at, end_at, mapped_style)
        if cuts and has_audio and not scenes_json:
            set_job(job_id, message="Detectando pausas para remover", progress=4)
            keep = detect_kept_segments(source, duration)
            if keep:
                keep = [(max(a, start_at), min(b, end_at)) for a,b in keep]
                keep = [(a,b) for a,b in keep if b-a >= 0.3]
                if keep:
                    scenes = [{"start": a, "end": b, "speed": 1, "look": mapped_style,
                               "zoom": "nenhum"} for a,b in keep]
        target = source.parent / "editado.mp4"
        set_job(job_id, state="processando", message="Renderizando cada cena, cor, som e efeitos", progress=8)
        render(FFMPEG, source, music, cover, target, scenes, duration, has_audio,
               intensity, audio_mode, voice_gain, music_gain, output_format,
               lambda p: set_job(job_id, progress=min(p,90) if subtitle_file else p))
        if subtitle_file:
            set_job(job_id, state="processando", progress=92,
                    message="Incorporando faixa de legendas SRT ao MP4")
            muxed = source.parent / "editado-com-legendas.mp4"
            mux = subprocess.run([
                FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                "-i", str(target), "-i", str(subtitle_file),
                "-map", "0:v:0", "-map", "0:a?", "-map", "1:s:0",
                "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                "-movflags", "+faststart", str(muxed)
            ], capture_output=True, text=True, timeout=900)
            if mux.returncode or not muxed.is_file():
                raise RuntimeError("Não foi possível incorporar as legendas: " + mux.stderr[-600:])
            muxed.replace(target)
        set_job(job_id, state="concluido", progress=100,
                message="Edição completa. Visualize e baixe seu MP4.",
                video=f"/api/jobs/{job_id}/video", size=target.stat().st_size)
    except Exception as exc:
        set_job(job_id, state="erro", message=str(exc)[:1900], progress=0)


def _caption_worker(task_id, source):
    try:
        set_job(task_id, state="processando", message="Transcrevendo áudio localmente", progress=10)
        out = source.parent / "legendas.srt"
        result = transcribe(FFMPEG, source, out)
        set_job(task_id, state="concluido", message=f"{result['segments']} legendas geradas.",
                progress=100, subtitles=f"/api/captions/{task_id}/srt")
    except Exception as exc:
        set_job(task_id, state="erro", message=str(exc)[:700], progress=0)


@app.post("/api/captions")
async def create_captions(video: UploadFile = File(...)):
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Selecione um vídeo válido.")
    task_id = uuid.uuid4().hex
    folder = DATA / task_id
    folder.mkdir(mode=0o700)
    path = folder / ("original" + ext)
    try:
        total = 0
        with path.open("wb") as out:
            while chunk := await video.read(1024*1024):
                total += len(chunk)
                if total > 400*1024*1024:
                    raise HTTPException(413, "A transcrição aceita vídeos até 400 MB.")
                out.write(chunk)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    with lock:
        jobs[task_id] = {"id":task_id,"state":"na_fila","progress":0,"message":"Aguardando transcrição"}
    threading.Thread(target=_caption_worker, args=(task_id,path),daemon=True).start()
    return {"id":task_id}


@app.get("/api/captions/{task_id}/srt")
def caption_file(task_id: str):
    with lock:
        job = jobs.get(task_id)
    path = DATA / task_id / "legendas.srt"
    if not job or job.get("state") != "concluido" or not path.exists():
        raise HTTPException(404, "Legendas ainda não disponíveis.")
    return FileResponse(path, media_type="application/x-subrip", filename="legendas.srt")


@app.post("/api/auto-plan")
async def auto_plan(video: UploadFile = File(...), style: str = Form("social")):
    if style not in {"social", "cinema", "igreja", "aula", "natural"}:
        raise HTTPException(400, "Estilo automático inválido.")
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Selecione um vídeo válido.")
    folder = DATA / ("analysis-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    source = folder / ("analise" + ext)
    try:
        total = 0
        with source.open("wb") as out:
            while chunk := await video.read(1024*1024):
                total += len(chunk)
                if total > 500*1024*1024:
                    raise HTTPException(413, "A análise automática aceita até 500 MB. Para arquivos maiores, use cortes manuais.")
                out.write(chunk)
        duration, has_audio = run_probe(source)
        # O plano é baseado em detecção de mudanças e silêncios, não semântica.
        scenes = await __import__("asyncio").to_thread(automatic_plan, FFMPEG, source, duration, has_audio, style)
        return {"scenes": scenes, "duration": duration, "method": "FFmpeg scdet + silencedetect"}
    except HTTPException:
        raise
    except (ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(422, str(exc))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@app.post("/api/jobs")
async def create_job(
    video: UploadFile = File(...),
    preset: str = Form("equilibrado"),
    remove_silence: bool = Form(False),
    music: UploadFile | None = File(None),
    cover: UploadFile | None = File(None),
    start_at: float = Form(0),
    end_at: float = Form(0),
    intensity: int = Form(65),
    scenes_json: str = Form(""),
    audio_mode: str = Form("voz"),
    voice_gain: float = Form(1.0),
    music_gain: float = Form(0.12),
    output_format: str = Form("original"),
    subtitle_file: UploadFile | None = File(None)
):
    if preset not in {"equilibrado", "dinamico", "suave"}:
        raise HTTPException(400, "Estilo inválido.")
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Formato de vídeo inválido.")
    mext = Path(music.filename or "").suffix.lower() if music and music.filename else ""
    if music and music.filename and mext not in AUDIO_EXTS:
        raise HTTPException(400, "Formato de música inválido.")
    cover_ext = Path(cover.filename or "").suffix.lower() if cover and cover.filename else ""
    if cover and cover.filename and cover_ext not in IMAGE_EXTS:
        raise HTTPException(400, "A capa deve ser JPG, PNG ou WebP.")
    if audio_mode not in {"original", "limpo", "voz"} or output_format not in {"original", "vertical", "horizontal", "quadrado"}:
        raise HTTPException(400, "Modo de áudio ou formato inválido.")
    if not 0 <= voice_gain <= 2 or not 0 <= music_gain <= 0.8:
        raise HTTPException(400, "Volumes fora da faixa permitida.")
    if not 0 <= intensity <= 100 or start_at < 0 or end_at < 0:
        raise HTTPException(400, "Parâmetros de edição inválidos.")
    await __import__("asyncio").to_thread(prune_old_files)
    job_id = uuid.uuid4().hex
    folder = DATA / job_id
    folder.mkdir(mode=0o700)
    src = folder / ("original" + ext)
    try:
        size = 0
        with src.open("wb") as f:
            while chunk := await video.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "O vídeo excede o limite configurado de 4 GB.")
                f.write(chunk)
        if size < 100:
            raise HTTPException(400, "Vídeo vazio.")
        soundtrack = None
        if music and music.filename:
            soundtrack = folder / ("trilha" + mext)
            music_size = 0
            with soundtrack.open("wb") as f:
                while chunk := await music.read(1024 * 1024):
                    music_size += len(chunk)
                    if music_size > 100 * 1024 * 1024:
                        raise HTTPException(413, "Limite de 100 MB para trilha.")
                    f.write(chunk)
        subtitle_path = None
        if subtitle_file and subtitle_file.filename:
            if Path(subtitle_file.filename).suffix.lower() != ".srt":
                raise HTTPException(400, "Envie legendas no formato .srt.")
            subtitle_path = folder / "legendas.srt"
            sub_size = 0
            with subtitle_path.open("wb") as f:
                while chunk := await subtitle_file.read(1024 * 1024):
                    sub_size += len(chunk)
                    if sub_size > 2*1024*1024:
                        raise HTTPException(413, "Limite de 2 MB para legendas.")
                    f.write(chunk)
        cover_path = None
        if cover and cover.filename:
            cover_path = folder / ("capa" + cover_ext)
            cover_size = 0
            with cover_path.open("wb") as f:
                while chunk := await cover.read(1024 * 1024):
                    cover_size += len(chunk)
                    if cover_size > 25 * 1024 * 1024:
                        raise HTTPException(413, "A capa deve ter no máximo 25 MB.")
                    f.write(chunk)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    with lock:
        jobs[job_id] = dict(id=job_id, state="na_fila", progress=0,
                            message="Aguardando processamento", video=None)
    threading.Thread(target=process, args=(job_id, src, soundtrack, cover_path, preset, remove_silence,
                           start_at, end_at, intensity, scenes_json, audio_mode,
                           voice_gain, music_gain, output_format, subtitle_path),
                     daemon=True).start()
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Trabalho não encontrado.")
        return dict(job)


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job or job["state"] != "concluido":
        raise HTTPException(404, "O vídeo ainda não está disponível.")
    path = DATA / job_id / "editado.mp4"
    if not path.is_file():
        raise HTTPException(404, "Arquivo não encontrado.")
    return FileResponse(path, media_type="video/mp4", filename="video-editado.mp4")


@app.get("/api/health")
def health():
    return {"ok": True, "engine": "ffmpeg"}


@app.get("/")
def home():
    return HTMLResponse(HTML)


HTML = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Editor de Vídeo Automático</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,Arial,sans-serif;background:#0a101c;color:#f2f5fb}
*{box-sizing:border-box}body{margin:0}header{padding:22px max(20px,calc((100vw - 1040px)/2));border-bottom:1px solid #26344d;display:flex;gap:12px;align-items:center}
.logo{background:#6477ff;border-radius:12px;padding:12px;font-size:22px}
main{max-width:1250px;margin:40px auto;padding:0 20px}
h1{font-size:clamp(30px,5vw,52px);letter-spacing:-1.7px;margin:0 0 10px}
p{color:#aab9d2;line-height:1.6}section{background:#141e30;border:1px solid #2b3b55;border-radius:20px;padding:24px;margin:22px 0}
label{display:block;font-weight:650;margin:16px 0 8px}
input[type=file],select{width:100%;padding:14px;background:#0c1423;color:white;border:1px solid #384964;border-radius:12px}
input[type=checkbox]{accent-color:#8796ff;width:18px;height:18px}
.check{display:flex;align-items:center;gap:12px;cursor:pointer}
.check label{margin:0;font-weight:500}
button,.download{border:0;border-radius:12px;padding:16px 24px;background:#7484ff;color:white;font-weight:750;cursor:pointer;display:inline-block;text-decoration:none;margin-top:20px}
button:disabled{opacity:.55;cursor:wait}
.small{font-size:13px;color:#91a3bc}
.track{height:13px;border-radius:9px;background:#263550;overflow:hidden;margin:18px 0}
.progress-bar{height:100%;width:0;background:linear-gradient(90deg,#6378ff,#5ad5da);transition:width .3s}
#status{overflow-wrap:anywhere}video{display:block;max-width:100%;width:100%;max-height:520px;border-radius:12px;background:black}
.features{display:flex;gap:10px;flex-wrap:wrap}.features span{padding:9px 13px;border:1px solid #344966;border-radius:99px;color:#b6c8e6}
.editor-grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(300px,.7fr);gap:20px;align-items:start}
.preview-pane{background:#0b1322;border:1px solid #2b3b55;border-radius:16px;padding:15px}
.editor-preview{max-height:430px;aspect-ratio:16/9;object-fit:contain;width:100%}
.controls{display:grid;grid-template-columns:1fr 1fr;gap:14px}
input[type=number],input[type=range]{width:100%}
input[type=number]{padding:13px;background:#0c1423;color:white;border:1px solid #384964;border-radius:10px}
input[type=range]{accent-color:#7484ff}
.scene{background:#0d1729;border:1px solid #324466;border-radius:12px;padding:13px;margin:12px 0}
.scene-head{display:flex;justify-content:space-between;align-items:center;gap:10px}
.scene h3{font-size:14px;margin:0;color:#c7d4ff}
.scene button{margin:0;padding:8px 10px;font-size:12px}
.scene-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.scene-cols label{font-size:12px}
.scene-cols input,.scene-cols select{width:100%;padding:9px;background:#141f33;border:1px solid #344966;border-radius:8px;color:white}
@media(max-width:480px){.scene-cols{grid-template-columns:1fr}}
.pill{color:#9ec1ff;border:1px solid #374b70;border-radius:10px;padding:8px 10px;display:inline-block;margin-top:12px}
@media(max-width:800px){.editor-grid{grid-template-columns:1fr}.controls{grid-template-columns:1fr}}
.hidden{display:none}footer{text-align:center;color:#8498b5;padding:28px}
</style></head><body>
<header><div class="logo">✦</div><strong>Editor de Vídeo Automático</strong></header>
<main>
<h1>Estúdio de edição de vídeo</h1>
<p>Prévia do material, corte por tempo, capa, filtros, zoom e melhoria automática — com controles inspirados em editores como o CapCut.</p>
<div class="features"><span>✓ Cor e nitidez</span><span>✓ Diretor automático</span><span>✓ Transições por cena</span><span>✓ Voz mais clara</span><span>✓ Zoom dinâmico</span><span>✓ Cortes de pausas</span><span>✓ MP4 pronto</span></div>
<div class="editor-grid"><section>
<form id="form">
<label for="video">Vídeo original *</label><input id="video" name="video" type="file" accept="video/*,.mkv,.avi" required>
<p class="small">MP4, MOV, MKV, AVI ou WebM · até 4 GB (sujeito ao limite da hospedagem)</p>
<div id="uploadStatus" aria-live="polite" style="margin:12px 0 22px">
<strong>1. Envio do vídeo para a plataforma</strong>
<div class="track" role="progressbar" aria-label="Envio do arquivo"><div id="uploadBar" class="progress-bar"></div></div>
<span id="uploadPercent">0% — aguardando envio</span>
</div>
<div class="controls">
<div><label for="start_at">✂️ Início do corte (segundos)</label><input type="number" min="0" step="0.1" value="0" id="start_at" name="start_at"></div>
<div><label for="end_at">✂️ Fim do corte (segundos)</label><input type="number" min="0" step="0.1" value="0" id="end_at" name="end_at"><p class="small">0 = até o final</p></div>
</div>
<label for="cover">🖼️ Adicionar capa (opcional)</label><input id="cover" name="cover" type="file" accept="image/png,image/jpeg,image/webp">
<p class="small">JPG, PNG ou WebP · a capa aparece nos 2 segundos iniciais do MP4</p>
<div style="border-top:1px solid #344966;margin:22px 0 14px;padding-top:14px"><h2>🎞️ Linha do tempo por cenas</h2>
<p class="small">Adicione até 30 trechos. Você pode dar a cada um cor, zoom e velocidade diferentes. Trechos são reproduzidos na ordem da lista.</p>
<div class="controls">
<div><label for="directorStyle">🎥 Estilo automático</label><select id="directorStyle">
<option value="social">Redes sociais — ritmo dinâmico</option>
<option value="cinema">Cinema — cortes suaves</option>
<option value="igreja">Igreja — edição respeitosa</option>
<option value="aula">Aula — foco na explicação</option>
<option value="natural">Natural — mínimo de alterações</option>
</select></div>
<div style="display:flex;align-items:end"><button type="button" id="autoDirector" style="width:100%;background:#596de0">✨ Diretor Automático</button></div></div>
<p class="small" id="directorStatus">Analisa mudanças visuais e pausas para sugerir trechos. Arquivos até 500 MB.</p>
<div id="sceneList"></div>
<div class="controls">
<button type="button" id="addScene" style="margin-top:10px;background:#32466e">+ Adicionar trecho</button>
<button type="button" id="clearScenes" style="margin-top:10px;background:#28364f">Usar vídeo inteiro</button>
<button type="button" id="saveProject" style="margin-top:10px;background:#264d55">💾 Salvar ajustes</button>
<button type="button" id="loadProject" style="margin-top:10px;background:#264d55">↶ Abrir ajustes salvos</button>
</div><p class="small" id="sceneSummary">Sem cortes por cenas: o vídeo inteiro será usado.</p></div>
<input type="hidden" name="scenes_json" id="scenes_json">
<label for="preset">Estilo da edição</label>
<select id="preset" name="preset"><option value="equilibrado">Profissional equilibrado</option><option value="dinamico">Dinâmico — cores vivas e zoom sutil</option><option value="suave">Suave — acabamento natural</option></select>
<label for="intensity">✨ Força da melhoria visual: <output id="intensityValue">65%</output></label>
<input type="range" id="intensity" name="intensity" min="0" max="100" value="65" step="5">
<p class="small">Aumente para destacar nitidez, contraste e cores (100% mais intenso). Prévia aproximada abaixo.</p>
<div class="check" style="margin-top:20px"><input id="remove_silence" name="remove_silence" type="checkbox" value="true"><label for="remove_silence">Remover pausas silenciosas longas (opcional)</label></div>
<h2>🎙️ Mixagem de áudio</h2>
<label for="audio_mode">Tratamento da voz / som original</label>
<select id="audio_mode" name="audio_mode">
<option value="voz">Voz destacada — redução de ruído + compressão</option>
<option value="limpo">Som limpo — tratamento equilibrado</option>
<option value="original">Áudio original — sem tratamento de ruído</option></select>
<label for="voice_gain">Volume da voz: <output id="voiceGainValue">100%</output></label>
<input type="range" id="voice_gain" name="voice_gain" min="0" max="2" value="1" step="0.05">
<label for="music_gain">Volume da música: <output id="musicGainValue">12%</output></label>
<input type="range" id="music_gain" name="music_gain" min="0" max="0.8" value="0.12" step="0.02">
<label for="output_format">📱 Formato do vídeo final</label>
<select id="output_format" name="output_format"><option value="original">Padrão HD 16:9</option>
<option value="vertical">Vertical 9:16 — Reels / Shorts</option>
<option value="horizontal">Horizontal 16:9 — YouTube</option>
<option value="quadrado">Quadrado 1:1 — Feed</option></select>
<label for="subtitle_file">💬 Incluir legendas SRT no MP4 (opcional)</label>
<input type="file" name="subtitle_file" id="subtitle_file" accept=".srt">
<p class="small">Legendas selecionáveis no player, não gravadas sobre a imagem. Use marcações de tempo relativas ao vídeo final; após cortes, a SRT original pode perder sincronização.</p>
<label for="music">Música de fundo (opcional)</label><input id="music" name="music" type="file" accept="audio/*">
<p class="small">A trilha enviada será misturada em volume baixo com a voz. Use músicas autorizadas.</p>
<button id="go" type="submit">✨ Melhorar vídeo automaticamente</button>
</form>
</section>
<div class="preview-pane">
<h2>🎬 Prévia do vídeo</h2>
<div style="overflow:hidden;border-radius:12px;background:#000">
<video id="localPreview" class="editor-preview" controls playsinline></video></div>
<div class="controls">
<button type="button" id="captureCover" style="background:#33486f;margin-top:12px">📸 Capturar quadro como capa</button>
<button type="button" id="toggleBefore" style="background:#33486f;margin-top:12px">👁️ Ver imagem original</button>
</div>
<p id="beforeLabel" class="small">Prévia com correção de cores aproximada.</p>
<p id="previewInfo" class="small">Selecione um vídeo para visualizar e definir o intervalo de corte.</p>
<div class="pill">Corte selecionado: <strong id="rangeLabel">Vídeo completo</strong></div>
<p class="small">Use o vídeo à esquerda para conferir o corte. Selecionar um trecho na linha do tempo move a reprodução para o início dele. O filtro da prévia é aproximado; a renderização aplica os efeitos de cada trecho.</p>
<div style="margin-top:24px;padding-top:12px;border-top:1px solid #344966">
<h2>💬 Legendas automáticas</h2>
<p class="small">Transcrição local em português para SRT. Requer instalar o módulo opcional de IA no servidor. Legendas são geradas com base no vídeo original.</p>
<button type="button" id="makeCaptions" style="width:100%;background:#33486f">Gerar legendas SRT do vídeo</button>
<p id="captionStatus" class="small">Selecione um vídeo para gerar legendas.</p>
<a id="captionDownload" class="download hidden" download="legendas.srt">Baixar legendas .SRT</a>
</div>
<img id="coverPreview" alt="Prévia da capa escolhida" class="hidden" style="width:100%;max-height:160px;object-fit:contain;margin-top:14px;border-radius:10px">
</div></div>
<section id="working" class="hidden" aria-live="polite">
<h2>2. Aplicando melhorias no vídeo</h2><p id="status">Aguardando upload...</p><div class="track"><div id="bar" class="progress-bar"></div></div><strong id="percent">0%</strong>
</section>
<section id="result" class="hidden"><h2>Vídeo finalizado ✓</h2><video id="preview" controls playsinline></video><a class="download" id="download">⬇ Baixar MP4 editado</a></section>
</main><footer>Processamento de vídeo com FFmpeg · Seu arquivo não é enviado a serviços externos de IA</footer>
<script>
const form=document.getElementById('form'),go=document.getElementById('go'),working=document.getElementById('working'),result=document.getElementById('result');
const statusEl=document.getElementById('status'),bar=document.getElementById('bar'),pct=document.getElementById('percent');
const uploadBar=document.getElementById('uploadBar'),uploadPct=document.getElementById('uploadPercent');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
document.getElementById('makeCaptions').addEventListener('click',async()=>{
 const file=document.getElementById('video').files[0];
 const status=document.getElementById('captionStatus'),btn=document.getElementById('makeCaptions');
 if(!file){status.textContent='Selecione o vídeo primeiro.';return}
 if(file.size>400*1024*1024){status.textContent='A transcrição aceita até 400 MB.';return}
 btn.disabled=true;status.textContent='Enviando vídeo para transcrição...';
 try{
  const data=new FormData();data.append('video',file);
  const r=await fetch('/api/captions',{method:'POST',body:data}),v=await r.json();
  if(!r.ok)throw Error(v.detail||'Falha ao iniciar');
  for(;;){
   await sleep(1500);
   const response=await fetch('/api/jobs/'+v.id,{cache:'no-store'});
   if(!response.ok)throw Error('Não foi possível acompanhar a transcrição.');
   const task=await response.json();
   status.textContent=task.message;
   if(task.state==='erro')throw Error(task.message);
   if(task.state==='concluido'){
    const link=document.getElementById('captionDownload');link.href=task.subtitles;
    link.classList.remove('hidden');break;
   }
  }
 }catch(e){status.textContent='Transcrição indisponível: '+e.message}
 finally{btn.disabled=false}
});
const sceneList=document.getElementById('sceneList');
let scenes=[];
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function drawScenes(){
 sceneList.innerHTML=scenes.map((sc,i)=>`
 <div class="scene" data-i="${i}" draggable="true" title="Arraste para mudar a ordem">
  <div class="scene-head"><h3>🎬 Trecho ${i+1}</h3><div><button type="button" data-action="preview" data-i="${i}" style="background:#415e8a">▶ Ver início</button>
  <button type="button" data-action="up" data-i="${i}" style="background:#334967" title="Subir trecho">↑</button>
  <button type="button" data-action="down" data-i="${i}" style="background:#334967" title="Descer trecho">↓</button>
  <button type="button" data-action="delete" data-i="${i}" style="background:#603d4b">Excluir</button></div></div>
  <div class="scene-cols">
   <div><label>Início (s)</label><input type="number" min="0" step="0.1" data-key="start" data-i="${i}" value="${sc.start}"></div>
   <div><label>Fim (s)</label><input type="number" min="0.3" step="0.1" data-key="end" data-i="${i}" value="${sc.end}"></div>
   <div><label>Cor</label><select data-key="look" data-i="${i}">
    ${[['natural','Natural'],['cinema','Cinema'],['vibrante','Vibrante'],['quente','Quente'],['frio','Frio'],['pb','Preto e branco']].map(([v,t])=>'<option value="'+v+'" '+(v===sc.look?'selected':'')+'>'+t+'</option>').join('')}
   </select></div>
   <div><label>Aproximação</label><select data-key="zoom" data-i="${i}">
    ${[['nenhum','Sem zoom'],['leve','Zoom leve'],['proximo','Zoom próximo']].map(([v,t])=>'<option value="'+v+'" '+(v===sc.zoom?'selected':'')+'>'+t+'</option>').join('')}
   </select></div>
   <div><label>Transição</label><select data-key="transition" data-i="${i}">
    ${[['cut','Corte seco'],['fade','Fade para preto']].map(([v,t])=>'<option value="'+v+'" '+(v===(sc.transition||'cut')?'selected':'')+'>'+t+'</option>').join('')}
   </select></div>
   <div><label>Velocidade</label><select data-key="speed" data-i="${i}">
    ${[['0.5','0,5x — lento'],['0.75','0,75x'],['1','1x — normal'],['1.25','1,25x'],['1.5','1,5x'],['2','2x — rápido']].map(([v,t])=>'<option value="'+v+'" '+(Number(v)===Number(sc.speed)?'selected':'')+'>'+t+'</option>').join('')}
   </select></div>
  </div></div>`).join('');
 const sum=scenes.reduce((a,x)=>a+Math.max(0,(x.end-x.start)/x.speed),0);
 document.getElementById('sceneSummary').textContent=scenes.length?
   scenes.length+' trecho(s) · previsão de '+sum.toFixed(1)+'s de vídeo final':
   'Sem trechos adicionais: o intervalo de corte principal será utilizado.';
 document.getElementById('scenes_json').value=scenes.length?JSON.stringify(scenes):'';
}
document.getElementById('addScene').addEventListener('click',()=>{
 if(scenes.length>=30){alert('Limite de 30 trechos.');return}
 const dur=Number(localPreview.duration)||30, last=scenes[scenes.length-1];
 const start=last?Math.min(dur-0.3,last.end):Number(startInput.value)||0;
 const end=Math.min(dur,start+Math.min(8,Math.max(0.3,dur-start)));
 if(end-start<0.3){alert('Não há duração suficiente para outro trecho.');return}
 scenes.push({start:Number(start.toFixed(2)),end:Number(end.toFixed(2)),speed:1,look:'vibrante',zoom:'nenhum'});
 drawScenes();
});
document.getElementById('clearScenes').addEventListener('click',()=>{scenes=[];drawScenes()});
let dragScene=-1;
sceneList.addEventListener('dragstart',e=>{
 const item=e.target.closest('.scene');if(!item)return;
 dragScene=Number(item.dataset.i);e.dataTransfer.effectAllowed='move';
});
sceneList.addEventListener('dragover',e=>{if(e.target.closest('.scene'))e.preventDefault()});
sceneList.addEventListener('drop',e=>{
 const target=e.target.closest('.scene');if(!target||dragScene<0)return;
 e.preventDefault();const to=Number(target.dataset.i);
 if(to!==dragScene){const moved=scenes.splice(dragScene,1)[0];scenes.splice(to,0,moved);drawScenes()}
 dragScene=-1;
});
sceneList.addEventListener('dragend',()=>{dragScene=-1});
document.getElementById('autoDirector').addEventListener('click',async()=>{
 const file=document.getElementById('video').files[0];
 const el=document.getElementById('directorStatus'),btn=document.getElementById('autoDirector');
 if(!file){el.textContent='Selecione um vídeo primeiro.';return}
 if(file.size>500*1024*1024){el.textContent='Para arquivos maiores que 500 MB, use cortes manuais.';return}
 btn.disabled=true;el.textContent='Analisando cenas e pausas na plataforma...';
 try{
  const p=new FormData();p.append('video',file);p.append('style',document.getElementById('directorStyle').value);
  const res=await fetch('/api/auto-plan',{method:'POST',body:p});
  const json=await res.json();
  if(!res.ok)throw Error(typeof json.detail==='string'?json.detail:'Falha na análise automática');
  scenes=json.scenes;drawScenes();
  el.textContent=scenes.length+' cenas sugeridas. Revise os cortes antes de exportar.';
 }catch(e){el.textContent='Erro na análise: '+e.message}
 finally{btn.disabled=false}
});
const STORAGE_KEY='editor-video-projeto-v2';
document.getElementById('saveProject').addEventListener('click',()=>{
 const keys=['start_at','end_at','preset','intensity','audio_mode','voice_gain','music_gain','output_format','remove_silence','directorStyle'];
 const options={};for(const id of keys){const el=document.getElementById(id);options[id]=el.type==='checkbox'?el.checked:el.value}
 try{localStorage.setItem(STORAGE_KEY,JSON.stringify({version:2,scenes,options}));
 document.getElementById('directorStatus').textContent='Ajustes salvos neste navegador. Arquivos de vídeo e música não são armazenados.'}
 catch(e){document.getElementById('directorStatus').textContent='Não foi possível salvar no navegador: '+e.message}
});
document.getElementById('loadProject').addEventListener('click',()=>{
 try{
  const data=JSON.parse(localStorage.getItem(STORAGE_KEY)||'null');
  if(!data){alert('Nenhum projeto salvo neste navegador.');return}
  scenes=Array.isArray(data.scenes)?data.scenes.slice(0,30):[];
  for(const [id,val] of Object.entries(data.options||{})){
   const el=document.getElementById(id);if(!el)continue;
   if(el.type==='checkbox')el.checked=Boolean(val);else el.value=val;
  }
  drawScenes();rangeUpdate();
  document.getElementById('directorStatus').textContent='Ajustes restaurados. Selecione novamente os arquivos de mídia.';
 }catch(e){alert('Não foi possível abrir o projeto: '+e.message)}
});
sceneList.addEventListener('click',e=>{
 const btn=e.target.closest('button[data-action]');if(!btn)return;
 const i=Number(btn.dataset.i);
 if(btn.dataset.action==='delete'){scenes.splice(i,1);drawScenes()}
 else if(btn.dataset.action==='up'&&i>0){[scenes[i-1],scenes[i]]=[scenes[i],scenes[i-1]];drawScenes()}
 else if(btn.dataset.action==='down'&&i<scenes.length-1){[scenes[i+1],scenes[i]]=[scenes[i],scenes[i+1]];drawScenes()}
 else if(btn.dataset.action==='preview'&&localPreview.readyState>0){localPreview.currentTime=scenes[i].start;localPreview.play().catch(()=>{})}
});
sceneList.addEventListener('change',e=>{
 const el=e.target;if(!el.dataset.key)return;
 const i=Number(el.dataset.i),k=el.dataset.key;
 scenes[i][k]=['start','end','speed'].includes(k)?Number(el.value):el.value;
 drawScenes();
});
[['voice_gain','voiceGainValue',100],['music_gain','musicGainValue',100]].forEach(([id,target,mul])=>{
 document.getElementById(id).addEventListener('input',e=>{
  document.getElementById(target).textContent=Math.round(Number(e.target.value)*mul)+'%';
 });
});
drawScenes();
const localPreview=document.getElementById('localPreview'),videoInput=document.getElementById('video');
const startInput=document.getElementById('start_at'),endInput=document.getElementById('end_at');
let localUrl=null,coverUrl=null;
let showOriginal=false;
document.getElementById('toggleBefore').addEventListener('click',()=>{
 showOriginal=!showOriginal;
 document.getElementById('beforeLabel').textContent=showOriginal?'Mostrando vídeo original, sem filtros.':'Mostrando prévia da correção visual.';
 previewCurrentLook();
});
document.getElementById('captureCover').addEventListener('click',async()=>{
 if(!localPreview.videoWidth){alert('Carregue um vídeo antes de capturar a capa.');return}
 const canvas=document.createElement('canvas');
 canvas.width=localPreview.videoWidth;canvas.height=localPreview.videoHeight;
 canvas.getContext('2d').drawImage(localPreview,0,0,canvas.width,canvas.height);
 canvas.toBlob(blob=>{
  if(!blob){alert('Não foi possível capturar o quadro.');return}
  const file=new File([blob],'capa-capturada.png',{type:'image/png'});
  const dt=new DataTransfer();dt.items.add(file);
  const input=document.getElementById('cover');input.files=dt.files;
  input.dispatchEvent(new Event('change'));
 },'image/png');
});
function rangeUpdate(){
 const start=Math.max(0,Number(startInput.value)||0);
 const end=Number(endInput.value)||localPreview.duration||0;
 document.getElementById('rangeLabel').textContent=start.toFixed(1)+'s → '+end.toFixed(1)+'s';
}
videoInput.addEventListener('change',()=>{
 if(localUrl)URL.revokeObjectURL(localUrl);
 const file=videoInput.files[0];if(!file)return;
 localUrl=URL.createObjectURL(file);localPreview.src=localUrl;
 document.getElementById('previewInfo').textContent=file.name+' · '+(file.size/1024/1024).toFixed(1)+' MB';
 startInput.value='0';endInput.value='0';rangeUpdate();
});
localPreview.addEventListener('loadedmetadata',()=>{
 endInput.value=localPreview.duration.toFixed(1);rangeUpdate();
});
[startInput,endInput].forEach(input=>input.addEventListener('change',()=>{
 rangeUpdate();if(input===startInput&&localPreview.readyState>0)localPreview.currentTime=Number(startInput.value)||0;
}));
function previewCurrentLook(){
 const scene=scenes.find(x=>localPreview.currentTime>=x.start&&localPreview.currentTime<x.end);
 if(showOriginal){localPreview.style.filter='none';localPreview.style.transform='none';return}
 const amt=Number(document.getElementById('intensity').value)/100;
 const looks={
  natural:[1.07,1.12,1,0],cinema:[1.24,.84,.97,0],
  vibrante:[1.18,1.52,1.01,0],quente:[1.11,1.19,1.03,0],
  frio:[1.14,1.11,1,0],pb:[1.21,0,1,1]
 };
 const [con,sat,bright,gray]=looks[scene?.look||'natural'];
 localPreview.style.filter='contrast('+(1+(con-1)*amt)+') saturate('+(1+(sat-1)*amt)+') brightness('+(1+(bright-1)*amt)+') grayscale('+(gray*amt)+')';
 const zoom=scene?.zoom==='proximo'?1.2:scene?.zoom==='leve'?1.06:1;
 localPreview.style.transform='scale('+zoom+')';
}
localPreview.addEventListener('timeupdate',()=>{
 if(!scenes.length&&Number(endInput.value)>0&&localPreview.currentTime>=Number(endInput.value))localPreview.pause();
 previewCurrentLook();
});
document.getElementById('cover').addEventListener('change',e=>{
 if(coverUrl)URL.revokeObjectURL(coverUrl);
 const file=e.target.files[0],img=document.getElementById('coverPreview');
 if(file){coverUrl=URL.createObjectURL(file);img.src=coverUrl;img.classList.remove('hidden')}
 else img.classList.add('hidden');
});
document.getElementById('intensity').addEventListener('input',e=>{
 const val=Number(e.target.value);
 document.getElementById('intensityValue').textContent=val+'%';
 previewCurrentLook();
});
function sendWithProgress(payload){
 return new Promise((resolve,reject)=>{
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/api/jobs');
  xhr.upload.onprogress=e=>{
   if(e.lengthComputable){
    const val=Math.min(99,Math.round(e.loaded*100/e.total));
    uploadBar.style.width=val+'%';
    uploadPct.textContent=val+'% — enviando arquivo';
   }else{uploadPct.textContent='Enviando vídeo...';}
  };
  xhr.onload=()=>{
   let data;try{data=JSON.parse(xhr.responseText)}catch{data={}}
   if(xhr.status>=200&&xhr.status<300){uploadBar.style.width='100%';uploadPct.textContent='100% — envio concluído';resolve(data)}
   else reject(Error(typeof data.detail==='string'?data.detail:'Falha no upload (HTTP '+xhr.status+'). Verifique o limite da hospedagem.'));
  };
  xhr.onerror=()=>reject(Error('Falha na conexão durante o upload.'));
  xhr.onabort=()=>reject(Error('Envio cancelado.'));
  xhr.send(payload);
 });
}
form.addEventListener('submit',async e=>{
 e.preventDefault();
 const selected=document.getElementById('video').files[0];
 if(!selected)return;
 if(selected.size>4*1024*1024*1024){uploadPct.textContent='Arquivo acima do limite de 4 GB.';return}
 const start=Number(startInput.value)||0,end=Number(endInput.value)||0;
 if(end>0&&end-start<0.3){statusEl.textContent='Defina um fim de corte posterior ao início.';working.classList.remove('hidden');return}
 if(localPreview.duration&&start>=localPreview.duration){statusEl.textContent='O início do corte ultrapassa a duração do vídeo.';working.classList.remove('hidden');return}
 if(scenes.some(x=>!Number.isFinite(x.start)||!Number.isFinite(x.end)||x.start<0||x.end-x.start<0.3||x.end>(localPreview.duration||Infinity)+0.15||x.speed<0.5||x.speed>2)){
  statusEl.textContent='Revise os tempos e velocidades dos trechos na linha do tempo.';working.classList.remove('hidden');return
 }
 go.disabled=true;result.classList.add('hidden');working.classList.remove('hidden');
 statusEl.textContent='Aguardando conclusão do envio...';bar.style.width='0%';pct.textContent='0%';
 uploadBar.style.width='0%';uploadPct.textContent='0% — preparando envio';
 try{
  const payload=new FormData(form);
  if(!document.getElementById('remove_silence').checked)payload.delete('remove_silence');
  const data=await sendWithProgress(payload);
  statusEl.textContent='Arquivo recebido. Iniciando melhorias...';
  for(;;){
   const r=await fetch('/api/jobs/'+data.id,{cache:'no-store'});
   if(!r.ok)throw Error('Não foi possível consultar o processamento.');
   const j=await r.json();
   statusEl.textContent=j.message;bar.style.width=j.progress+'%';pct.textContent=j.progress+'%';
   if(j.state==='erro')throw Error(j.message);
   if(j.state==='concluido'){
    document.getElementById('preview').src=j.video;
    document.getElementById('download').href=j.video;
    result.classList.remove('hidden');break;
   }
   await sleep(1200);
  }
 }catch(err){statusEl.textContent='Erro: '+err.message}
 finally{go.disabled=false}
});
</script></body></html>
""";


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("APP_PORT", "8080")))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
