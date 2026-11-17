"""Assistente de edição automática com detecção de cortes e análise de áudio local.

Não utiliza serviços pagos nem finge reconhecer o conteúdo sem modelo de visão.
"""
import re
import subprocess
import math


def _run(ffmpeg, args, timeout=180):
    p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.stderr


def detect_scene_cuts(ffmpeg, source, duration, threshold=0.34, max_scenes=18):
    """Busca transições visuais por mudança de frames (FFmpeg scdet)."""
    duration = min(float(duration), 1800)
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
                            "-t", str(duration), "-vf",
                            f"scale=320:-2,select=gt(scene\\,{threshold:.3f}),showinfo",
                            "-an", "-f", "null", "-"], capture_output=True,
                           text=True, timeout=max(60, min(1800, duration*3)))
        times = [float(x) for x in re.findall(r"showinfo.*?pts_time:\s*([\d.]+)", p.stderr)]

    except (subprocess.TimeoutExpired, ValueError):
        times = []
    points = [0.0]
    for v in sorted(times):
        if v - points[-1] >= 2.0 and v < duration - 2:
            points.append(v)
        if len(points) >= max_scenes:
            break
    points.append(duration)
    return [(a, b) for a,b in zip(points, points[1:]) if b-a >= 0.3]


def silence_gaps(ffmpeg, source, duration, threshold="-35dB", min_length=0.75):
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
                            "-t", str(min(duration,1800)), "-af",
                            f"silencedetect=noise={threshold}:d={min_length}",
                            "-vn", "-f", "null", "-"], capture_output=True,
                           text=True, timeout=max(60,min(1800,duration*3)))
        starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)",p.stderr)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)",p.stderr)]
    except (subprocess.TimeoutExpired, ValueError):
        return []
    return [(a, ends[i] if i<len(ends) else duration) for i,a in enumerate(starts)]


def automatic_plan(ffmpeg, source, duration, has_audio, style="social"):
    """Plano transparente e determinístico: cortes por cena, pausas e estilos variados."""
    if style not in {"social", "cinema", "igreja", "aula", "natural"}:
        raise ValueError("Estilo do diretor automático inválido.")
    cuts = detect_scene_cuts(ffmpeg, source, duration)
    gaps = silence_gaps(ffmpeg, source, duration) if has_audio else []
    scenes = []
    looks = {
        "social": ["vibrante","cinema","vibrante","quente"],
        "cinema": ["cinema","quente","frio","cinema"],
        "igreja": ["natural","quente","natural","cinema"],
        "aula": ["natural","natural","vibrante","natural"],
        "natural": ["natural"]
    }[style]
    for start,end in cuts:
        chunks=[(start,end)]
        if style in {"social","aula"} and has_audio:
            for a,b in gaps:
                if b-a < 1 or b<=start or a>=end:
                    continue
                a=max(start,a+0.15)
                b=min(end,b-0.15)
                next_chunks=[]
                for lo,hi in chunks:
                    if hi<=a or lo>=b:next_chunks.append((lo,hi));continue
                    if a-lo>=0.6:next_chunks.append((lo,a))
                    if hi-b>=0.6:next_chunks.append((b,hi))
                if next_chunks:chunks=next_chunks
        for lo,hi in chunks:
            if hi-lo < 0.5:continue
            i=len(scenes)
            scenes.append({"start":round(lo,3),"end":round(hi,3),
                           "look":looks[i%len(looks)],
                           "zoom":("leve" if style=="social" and i%3==1 else "nenhum"),
                           "speed":1,"label":f"Cena {i+1}",
                           "transition":"fade" if i>0 and style in {"cinema","igreja"} else "cut"})
            if len(scenes)>=30:break
        if len(scenes)>=30:break
    if not scenes:
        scenes=[{"start":0,"end":duration,"look":"natural","zoom":"nenhum",
                 "speed":1,"transition":"cut","label":"Vídeo completo"}]
    return scenes
