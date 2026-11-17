"""Motor de renderização por cenas para o editor de vídeo.

Cada trecho recebe filtros próprios e os trechos são concatenados em um MP4.
Sem serviços externos, sem prometer reconstrução por IA.
"""
import json
import math
import re
import subprocess
import threading
from pathlib import Path

ALLOWED_LOOKS = {"natural", "cinema", "vibrante", "quente", "frio", "pb"}
ALLOWED_ZOOMS = {"nenhum", "leve", "proximo"}
ALLOWED_FORMATS = {"original", "vertical", "horizontal", "quadrado"}
ALLOWED_TRANSITIONS = {"cut", "fade"}


def clamp(value, low, high):
    return max(low, min(high, value))


def parse_scenes(raw, duration, start_at, end_at, default_style):
    if not raw:
        return [{"start": start_at, "end": end_at, "look": default_style,
                 "zoom": "nenhum", "speed": 1.0, "label": "Vídeo", "transition": "cut"}]
    try:
        scenes = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("A lista de trechos não está em formato JSON válido.")
    if not isinstance(scenes, list) or not (1 <= len(scenes) <= 30):
        raise ValueError("Informe entre 1 e 30 trechos.")
    result = []
    total_seconds = 0
    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError("Um trecho não está no formato esperado.")
        try:
            start = float(scene.get("start"))
            end = float(scene.get("end"))
            speed = float(scene.get("speed", 1))
        except (ValueError, TypeError):
            raise ValueError(f"Tempo ou velocidade inválidos no trecho {i+1}.")
        if not all(math.isfinite(x) for x in (start, end, speed)):
            raise ValueError("Os tempos devem ser números finitos.")
        if start < 0 or end > duration + 0.15 or end - start < 0.3:
            raise ValueError(f"Trecho {i+1} está fora da duração ou é muito curto.")
        if not 0.5 <= speed <= 2:
            raise ValueError("A velocidade deve ficar entre 0,5x e 2x.")
        look = scene.get("look", default_style)
        zoom = scene.get("zoom", "nenhum")
        transition = scene.get("transition", "cut")
        if look not in ALLOWED_LOOKS or zoom not in ALLOWED_ZOOMS or transition not in ALLOWED_TRANSITIONS:
            raise ValueError("Efeito não reconhecido em um dos trechos.")
        result.append({"start": start, "end": min(duration, end),
                       "speed": speed, "look": look, "zoom": zoom,
                       "label": str(scene.get("label", "Cena"))[:60],
                       "transition": transition})
        total_seconds += (end - start) / speed
    if total_seconds > 4 * 3600:
        raise ValueError("O projeto não pode ultrapassar quatro horas de saída.")
    return result


def video_filters(look, zoom, intensity, output_format):
    amt = clamp(intensity, 0, 100) / 100
    filters = ["fps=30"]
    # Choices deliberately have visibly different color grades.
    looks = {
        "natural": (1.07, 1.12, 0.006, 0),
        "cinema": (1.24, 0.84, -0.012, 0),
        "vibrante": (1.18, 1.52, 0.01, 0),
        "quente": (1.11, 1.19, 0.015, 0.055),
        "frio": (1.14, 1.11, 0.001, -0.055),
        "pb": (1.21, 0, 0.005, 0),
    }
    con, sat, bright, gamma = looks[look]
    if amt:
        filters.append("hqdn3d=1.6:1.4:5:4")
        filters.append(
            f"eq=contrast={1+(con-1)*amt:.3f}:saturation={1+(sat-1)*amt:.3f}:"
            f"brightness={bright*amt:.3f}:gamma_r={1+gamma*amt:.3f}:"
            f"gamma_b={1-gamma*amt:.3f}")
        filters.append(f"unsharp=5:5:{0.75*amt:.3f}:3:3:{0.15*amt:.3f}")
    if zoom == "leve":
        filters.append("crop=iw*0.94:ih*0.94:(iw-ow)/2:(ih-oh)/2")
    elif zoom == "proximo":
        filters.append("crop=iw*0.82:ih*0.82:(iw-ow)/2:(ih-oh)/2")
    if output_format == "vertical":
        filters.append("scale=720:1280:force_original_aspect_ratio=decrease")
        filters.append("pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black")
    elif output_format == "horizontal":
        filters.append("scale=1280:720:force_original_aspect_ratio=decrease")
        filters.append("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black")
    elif output_format == "quadrado":
        filters.append("scale=900:900:force_original_aspect_ratio=decrease")
        filters.append("pad=900:900:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        # Prevent enormous intermediate files and align concat dimensions.
        filters.append("scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease")
        filters.append("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black")
    filters.extend(["setsar=1", "format=yuv420p"])
    return ",".join(filters)


def audio_filters(mode, gain):
    gain = clamp(float(gain), 0, 2)
    if mode == "original":
        return f"volume={gain:.3f}"
    if mode == "voz":
        return (f"highpass=f=90,lowpass=f=10500,afftdn=nf=-28,"
                f"acompressor=threshold=-25dB:ratio=3.6:attack=10:release=170,"
                f"volume={gain:.3f},loudnorm=I=-16:TP=-1.5:LRA=9")
    return (f"highpass=f=65,lowpass=f=15500,afftdn=nf=-24,"
            f"acompressor=threshold=-21dB:ratio=2.5:attack=12:release=200,"
            f"volume={gain:.3f},loudnorm=I=-16:TP=-1.5:LRA=11")


def render(ffmpeg, source, music, cover, output, scenes, duration, has_audio,
           intensity, audio_mode, voice_gain, music_gain, output_format, progress_cb):
    if output_format not in ALLOWED_FORMATS:
        raise ValueError("Formato de saída inválido.")
    if audio_mode not in {"original", "limpo", "voz"}:
        raise ValueError("Tratamento de voz inválido.")
    filters = []
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-i", str(source)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    # Overlay at the beginning without blocking timeline.
    cover_index = 2 if music else 1
    if cover:
        cmd += ["-loop", "1", "-framerate", "30", "-i", str(cover)]
    out_length = sum((s["end"] - s["start"]) / s["speed"] for s in scenes)
    for i, scene in enumerate(scenes):
        start, end, speed = scene["start"], scene["end"], scene["speed"]
        vf = video_filters(scene["look"], scene["zoom"], intensity, output_format)
        clip_length = (end-start)/speed
        if scene.get("transition") == "fade" and i > 0 and clip_length > 1:
            vf += ",fade=t=in:st=0:d=0.35"
        if scene.get("transition") == "fade" and i < len(scenes)-1 and clip_length > 1:
            vf += f",fade=t=out:st={max(0,clip_length-0.35):.3f}:d=0.35"
        filters.append(
            f"[0:v]trim=start={start:.4f}:end={end:.4f},setpts=(PTS-STARTPTS)/{speed:.4f},"
            f"{vf}[v{i}]"
        )
        if has_audio:
            filters.append(
                f"[0:a]atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS,"
                f"atempo={speed:.4f},aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
            )
    if has_audio:
        joins = "".join(f"[v{i}][a{i}]" for i in range(len(scenes)))
        filters.append(f"{joins}concat=n={len(scenes)}:v=1:a=1[joinedv][joineda]")
    else:
        joins = "".join(f"[v{i}]" for i in range(len(scenes)))
        filters.append(f"{joins}concat=n={len(scenes)}:v=1:a=0[joinedv]")
    if cover:
        # Use a picture as first 2 seconds, resized to the fixed canvas.
        width, height = {"vertical": (720,1280), "quadrado": (900,900)}.get(output_format, (1280,720))
        filters.append(
            f"[{cover_index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p[c]"
        )
        filters.append("[joinedv][c]overlay=0:0:enable='lt(t,2)':shortest=1:repeatlast=0[outv]")
    else:
        filters.append("[joinedv]null[outv]")
    if has_audio:
        filters.append(f"[joineda]{audio_filters(audio_mode, voice_gain)}[voice]")
    if music:
        filters.append(f"[1:a]volume={clamp(float(music_gain),0,0.8):.3f},aresample=48000[music]")
        if has_audio:
            filters.append("[music][voice]sidechaincompress=threshold=0.05:ratio=5:attack=40:release=350[ducked]")
            filters.append("[voice][ducked]amix=inputs=2:duration=first:dropout_transition=1[outa]")
        else:
            filters.append("[music]anull[outa]")
    elif has_audio:
        filters.append("[voice]anull[outa]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[outv]"]
    if has_audio or music:
        cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t",
            f"{out_length:.4f}", str(output)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    err_chunks = []
    def read_errors():
        for line in proc.stderr:
            err_chunks.append(line)
            if len(err_chunks) > 100:
                err_chunks.pop(0)
    thread = threading.Thread(target=read_errors, daemon=True)
    thread.start()
    for line in proc.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                elapsed = int(line.split("=", 1)[1]) / 1000000
                progress_cb(int(clamp(8 + 90 * elapsed / max(out_length,0.1), 8, 98)))
            except ValueError:
                pass
    code = proc.wait()
    thread.join(timeout=3)
    if code != 0 or not Path(output).is_file() or Path(output).stat().st_size < 1024:
        raise RuntimeError("Falha na renderização: " + "".join(err_chunks)[-1800:])
    progress_cb(100)
    return out_length
