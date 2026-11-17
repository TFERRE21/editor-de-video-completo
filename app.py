import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import imageio_ffmpeg

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Editor de Vídeo Automático")
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


def process(job_id, source, music, cover, preset, cuts, start_at, end_at, intensity):
    try:
        set_job(job_id, state="analisando", message="Analisando vídeo", progress=2)
        duration, has_audio = run_probe(source)
        start_at = max(0.0, min(float(start_at), duration - 0.1))
        end_at = float(end_at) if float(end_at) > 0 else duration
        end_at = min(end_at, duration)
        if end_at - start_at < 0.3:
            raise ValueError("Selecione um intervalo de pelo menos 0,3 segundo.")
        segments = None
        if cuts and has_audio:
            set_job(job_id, message="Detectando pausas silenciosas", progress=4)
            segments = detect_kept_segments(source, duration)
            if segments:
                segments = [(max(a, start_at), min(b, end_at)) for a,b in segments]
                segments = [(a,b) for a,b in segments if b-a >= 0.3]
                if not segments:
                    raise ValueError("Os cortes automáticos removeriam todo o conteúdo; desative a opção de pausas.")
        vf, af = make_filters(preset, has_audio, intensity)
        target = source.parent / "editado.mp4"
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
               "-progress", "pipe:1", "-i", str(source)]
        if music:
            cmd += ["-stream_loop", "-1", "-i", str(music)]
        if cover:
            cmd += ["-loop", "1", "-framerate", "25", "-i", str(cover)]
        cover_index = 2 if music else 1
        filter_steps = []
        if segments:
            for i, (start, end) in enumerate(segments):
                filter_steps += [
                    f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]",
                    f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
                ]
            joined = "".join(f"[v{i}][a{i}]" for i in range(len(segments)))
            filter_steps.append(f"{joined}concat=n={len(segments)}:v=1:a=1[vc][ac]")
            vin, ain = "[vc]", "[ac]"
            output_duration = sum(end - start for start, end in segments)
        else:
            vin, ain, output_duration = "[vc]", "[ac]", end_at - start_at
            filter_steps.append(f"[0:v]trim=start={start_at:.3f}:end={end_at:.3f},setpts=PTS-STARTPTS[vc]")
            if has_audio:
                filter_steps.append(f"[0:a]atrim=start={start_at:.3f}:end={end_at:.3f},asetpts=PTS-STARTPTS[ac]")
        if cover:
            # A capa substitui os primeiros 2 segundos da imagem, sem esconder o áudio.
            filter_steps.append(f"{vin}{vf}[graded]")
            filter_steps.append(
                f"[{cover_index}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2[coverraw]"
            )
            filter_steps.append("[coverraw][graded]scale2ref=w=iw:h=ih[coverfit][gradedref]")
            filter_steps.append("[gradedref][coverfit]overlay=0:0:enable='lt(t,2)':shortest=1:repeatlast=0[vout]")
        else:
            filter_steps.append(f"{vin}{vf}[vout]")
        if has_audio:
            filter_steps.append(f"{ain}{af}[clean]")
            if music:
                filter_steps.append("[1:a]volume=0.10[bg]")
                filter_steps.append("[clean][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]")
            else:
                filter_steps.append("[clean]anull[aout]")
        elif music:
            filter_steps.append("[1:a]volume=0.14,loudnorm=I=-22:TP=-2:LRA=12[aout]")
        cmd += ["-filter_complex", ";".join(filter_steps), "-map", "[vout]"]
        if has_audio or music:
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest",
                str(target)]
        set_job(job_id, state="processando", message="Aplicando efeitos e renderizando MP4", progress=8)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # stderr can be read after completion; error log remains small (-loglevel error).
        for line in proc.stdout:
            if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                try:
                    micro = int(line.split("=", 1)[1])
                    pct = min(98, max(8, int(8 + 90 * (micro / 1_000_000) / output_duration)))
                    set_job(job_id, progress=pct)
                except (ValueError, ZeroDivisionError):
                    pass
        error = proc.stderr.read()
        if proc.wait() != 0 or not target.exists() or target.stat().st_size < 1024:
            raise RuntimeError(error[-1400:] or "FFmpeg não conseguiu finalizar o vídeo.")
        set_job(job_id, state="concluido", progress=100,
                message="Vídeo editado e pronto para baixar", video=f"/api/jobs/{job_id}/video",
                size=target.stat().st_size)
    except Exception as exc:
        set_job(job_id, state="erro", message=str(exc)[:1800], progress=0)


@app.post("/api/jobs")
async def create_job(
    video: UploadFile = File(...),
    preset: str = Form("equilibrado"),
    remove_silence: bool = Form(False),
    music: UploadFile | None = File(None),
    cover: UploadFile | None = File(None),
    start_at: float = Form(0),
    end_at: float = Form(0),
    intensity: int = Form(65)
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
    if not 0 <= intensity <= 100 or start_at < 0 or end_at < 0:
        raise HTTPException(400, "Parâmetros de edição inválidos.")
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
    threading.Thread(target=process, args=(job_id, src, soundtrack, cover_path, preset, remove_silence, start_at, end_at, intensity),
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
main{max-width:1040px;margin:40px auto;padding:0 20px}
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
.hidden{display:none}footer{text-align:center;color:#8498b5;padding:28px}
</style></head><body>
<header><div class="logo">✦</div><strong>Editor de Vídeo Automático</strong></header>
<main>
<h1>Seu vídeo, com acabamento profissional.</h1>
<p>Envie o arquivo, selecione o estilo e acompanhe a edição automática. Sem timeline ou ajustes manuais.</p>
<div class="features"><span>✓ Cor e nitidez</span><span>✓ Voz mais clara</span><span>✓ Zoom dinâmico</span><span>✓ Cortes de pausas</span><span>✓ MP4 pronto</span></div>
<section>
<form id="form">
<label for="video">Vídeo original *</label><input id="video" name="video" type="file" accept="video/*,.mkv,.avi" required>
<p class="small">MP4, MOV, MKV, AVI ou WebM · até 4 GB (sujeito ao limite da hospedagem)</p>
<div id="uploadStatus" aria-live="polite" style="margin:12px 0 22px">
<strong>1. Envio do vídeo para a plataforma</strong>
<div class="track" role="progressbar" aria-label="Envio do arquivo"><div id="uploadBar" class="progress-bar"></div></div>
<span id="uploadPercent">0% — aguardando envio</span>
</div>
<label for="preset">Estilo da edição</label>
<select id="preset" name="preset"><option value="equilibrado">Profissional equilibrado</option><option value="dinamico">Dinâmico — cores vivas e zoom sutil</option><option value="suave">Suave — acabamento natural</option></select>
<div class="check" style="margin-top:20px"><input id="remove_silence" name="remove_silence" type="checkbox" value="true"><label for="remove_silence">Remover pausas silenciosas longas (opcional)</label></div>
<label for="music">Música de fundo (opcional)</label><input id="music" name="music" type="file" accept="audio/*">
<p class="small">A trilha enviada será misturada em volume baixo com a voz. Use músicas autorizadas.</p>
<button id="go" type="submit">✨ Melhorar vídeo automaticamente</button>
</form>
</section>
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
