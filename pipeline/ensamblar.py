"""Ensamblaje con ffmpeg: Ken Burns por escena (o el clip propio), fundidos entre escenas, portada,
voz con pausas, música, subtítulos, marca de agua y cierre."""
import subprocess
import os
import tempfile
from pathlib import Path

from .guion import EXT_CLIP, Guion
from . import marca
from .marca import CIERRE_SEG
from .musica import MOODS, mezclar, resolver
from .subtitulos import filtro as filtro_subtitulos
from .textos import ErrorEjecucion, Texto, decir
from . import rutas

FPS = 30
XFADE = 0.5  # segundos de fundido entre escenas
CIERRE_FUNDIDO = 0.6  # segundos de fundido de la última escena a la tarjeta de cierre
PORTADA_SEG = 0.6  # la miniatura como primer cuadro: la voz empieza cuando termina
PORTADA_FUNDIDO = 0.2  # fundido rápido a la escena 1, dentro de esos 0,6 s: la voz arranca sobre la escena limpia
RAIZ = Path(__file__).parent.parent


def _run(args: list[str], cwd: Path | None = None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ErrorEjecucion(Texto("err.ffmpeg", detalle=r.stderr[-3000:]))


def _clip_escena(img: Path, dur: float, idx: int, size: tuple[int, int], salida: Path):
    """Un mp4 mudo por escena con movimiento de cámara suave. Dura dur + XFADE (cola para el fundido)."""
    w, h = size
    frames = int(round((dur + XFADE) * FPS))
    # escalamos la imagen grande antes de zoompan para evitar el temblor típico
    pre = f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,crop={w*2}:{h*2}"
    z_in = f"1+0.10*on/{frames}"
    z_out = f"1.10-0.10*on/{frames}"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    patrones = [
        (z_in, cx, cy),                                   # zoom in centrado
        (z_out, cx, cy),                                  # zoom out centrado
        (z_in, f"(iw-iw/zoom)*on/{frames}", cy),          # zoom in + paneo horizontal
        (z_out, cx, f"(ih-ih/zoom)*(1-on/{frames})"),     # zoom out + paneo vertical
    ]
    z, x, y = patrones[(idx - 1) % len(patrones)]
    vf = (
        f"{pre},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS},"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(img),
        "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(salida),
    ])


def _clip_propio(video: Path, dur: float, size: tuple[int, int], salida: Path, inicio: float = 0.0):
    """Un clip propio recortado al formato y ajustado a dur + XFADE: si es más largo se corta, si es
    más corto se congela el último cuadro. Sin su audio."""
    from .medios import validar_inicio, inicio_decodificable, info_clip
    info = validar_inicio(video, inicio)
    seek = inicio_decodificable(video, inicio, info)
    w, h = size
    largo = dur + XFADE
    # 'dar' incluye SAR: también los videos con píxeles no cuadrados conservan su aspecto.
    vf = (f"setpts=PTS-STARTPTS,scale=w='if(gt(dar,{w}/{h}),ceil({h}*dar/2)*2,{w})':"
          f"h='if(gt(dar,{w}/{h}),{h},ceil({w}/dar/2)*2)',crop={w}:{h},setsar=1,fps={FPS},"
          f"format=yuv420p,tpad=stop_mode=clone:stop_duration={largo:.3f}")
    fd, nombre = tempfile.mkstemp(dir=salida.parent, prefix='.clip-', suffix='.mp4')
    os.close(fd)
    temporal = Path(nombre)
    try:
        _run([
            "ffmpeg", "-y", "-v", "error", "-ss", f"{seek:.6f}",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-i", str(video),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
            "-frames:v", str(int(round(largo * FPS))), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(temporal),
        ])
        render = info_clip(temporal)
        if ((render['ancho'], render['alto']) != size or abs(render['duracion'] - largo) > .1):
            from .textos import error
            raise error('clip.decodificacion')
        os.replace(temporal, salida)
    finally:
        temporal.unlink(missing_ok=True)


def _clip_portada(img: Path, size: tuple[int, int], salida: Path):
    """La miniatura quieta durante PORTADA_SEG (el fundido a la escena 1 son sus últimos PORTADA_FUNDIDO s)."""
    w, h = size
    frames = int(round(PORTADA_SEG * FPS))
    _run([
        "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(img),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS},format=yuv420p",
        "-frames:v", str(frames), "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(salida),
    ])


def _unir_video(clips: list[Path], duraciones: list[float], salida: Path, fundidos: list[float] | None = None):
    """Encadena los clips con xfade. Cada clip trae de cola lo que dura su fundido con el siguiente
    (XFADE salvo que 'fundidos' diga otra cosa), así la voz queda sincronizada."""
    fundidos = fundidos or [XFADE] * (len(clips) - 1)
    if len(clips) == 1:
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(clips[0]), "-t", f"{duraciones[0]:.3f}",
              "-c", "copy", str(salida)])
        return
    args = ["ffmpeg", "-y", "-v", "error"]
    for c in clips:
        args += ["-i", str(c)]
    filtros, prev, acumulado = [], "[0:v]", 0.0
    for i in range(1, len(clips)):
        acumulado += duraciones[i - 1]
        out = f"[v{i}]"
        filtros.append(f"{prev}[{i}:v]xfade=transition=fade:duration={fundidos[i - 1]}:offset={acumulado:.3f}{out}")
        prev = out
    total = sum(duraciones)
    args += ["-filter_complex", ";".join(filtros), "-map", prev, "-t", f"{total:.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(salida)]
    _run(args)


def _pista_voz(voces: list[Path], tramos: list[float], salida: Path, silencio_inicial: float = 0.0):
    """Las voces una tras otra, cada una rellenada con silencio hasta su tramo (voz + pausa), con
    'silencio_inicial' delante (la portada). Por filtro y no con concat de mp3: así cada tramo dura
    exactamente lo que dura su escena en el video."""
    args, partes, filtros = ["ffmpeg", "-y", "-v", "error"], [], []
    fmt = "aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono"
    if silencio_inicial > 0:
        args += ["-f", "lavfi", "-t", f"{silencio_inicial:.3f}", "-i", "anullsrc=r=24000:cl=mono"]
        filtros.append(f"[0:a]{fmt}[s0]")
        partes.append("[s0]")
    base = len(partes)
    for i, (v, t) in enumerate(zip(voces, tramos)):
        args += ["-i", v.name]
        filtros.append(f"[{base + i}:a]{fmt},apad,atrim=0:{t:.3f},asetpts=N/SR/TB[t{i}]")
        partes.append(f"[t{i}]")
    filtros.append("".join(partes) + f"concat=n={len(partes)}:v=0:a=1[voz]")
    args += ["-filter_complex", ";".join(filtros), "-map", "[voz]", "-c:a", "aac", "-b:a", "192k", salida.name]
    _run(args, cwd=salida.parent)


def _unir_audio(voces: list[Path], tramos: list[float], salida: Path, musica: Path | None, vol_musica: float,
                duracion: float, silencio_inicial: float = 0.0):
    voz = salida.parent / "voz_completa.m4a"
    _pista_voz(voces, tramos, voz, silencio_inicial)
    if musica and musica.exists():
        decir("log.musica", pista=musica.relative_to(rutas.DATOS) if musica.is_relative_to(rutas.DATOS) else musica, vol=vol_musica)
        mezclar(voz, musica, salida, vol_musica, duracion)
    else:
        if musica:
            decir("log.musica_falta", pista=musica)
        salida.write_bytes(voz.read_bytes())


def duracion_prevista(g: Guion, voces: list[dict], limite: float | None = None,
                      cierre: bool = True, portada: bool = False) -> float:
    """Mismo tiempo objetivo (-t) del render: voces reales, pausas, portada y cierre.

    Los fundidos usan colas de los clips y no alargan el resultado.
    """
    tramos = [round(v["duracion"] + e.pausa, 3) for e, v in zip(g.escenas, voces)]
    voz = sum(tramos)
    return ((PORTADA_SEG if portada else 0.0)
            + (voz if limite is None else min(limite, voz))
            + (CIERRE_SEG if cierre else 0.0))


def ensamblar(g: Guion, carpeta: Path, imagenes: list[Path], voces: list[dict], ass: Path,
              subtitulos: bool = True, destino: Path | None = None, limite: float | None = None,
              marca_agua: bool = True, cierre: bool = True, portada: Path | None = None,
              estilo_marca: marca.EstiloMarca | None = None, prueba: bool = False,
              logo_marca: Path | None = None, tarjeta: Path | None = None) -> Path:
    """destino: MP4 alternativo, también fuera de carpeta; relativo al directorio del llamador.
    limite: para renders parciales (p. ej. "probar mezcla": solo los primeros segundos).
    marca_agua: logo al 55 % en la esquina, el tamaño y el margen de estilo_marca. cierre: tarjeta de CIERRE_SEG s tras la última escena
    ('tarjeta' si se da; si no, la de siempre: logo y "Dcrypto Historias").
    imagenes: una por escena; un .mp4/.mov es un clip propio y va tal cual, sin movimiento de cámara.
    portada: la miniatura, PORTADA_SEG s antes de la escena 1 (el .ass ya tiene que venir desplazado).
    prueba: modo prueba de la licencia: 'Dcrypto Studio TRIAL' en el centro de todo el video, cierre incluido."""
    clips_dir = carpeta / "clips"
    clips_dir.mkdir(exist_ok=True)
    # cada escena dura su voz más su pausa
    duraciones = [round(v["duracion"] + e.pausa, 3) for e, v in zip(g.escenas, voces)]
    # con portada, la escena 1 entra durante el fundido: su clip empieza PORTADA_FUNDIDO s antes que su voz
    dur_clips = [d + (PORTADA_FUNDIDO if portada and i == 0 else 0) for i, d in enumerate(duraciones)]

    decir("log.clips")
    clips = []
    for e, img, dur in zip(g.escenas, imagenes, dur_clips):
        propio = img.suffix.lower() in EXT_CLIP
        # la duración va en el nombre: si cambia la voz, el clip se rehace con el largo nuevo
        inicio = f'_v2_s{e.inicio_clip:.3f}' if propio else ''
        clip = clips_dir / f"clip_{e.idx:02d}_{img.stem}{inicio}_{dur:.2f}_{g.tamano[0]}x{g.tamano[1]}.mp4"
        if not clip.exists():
            if propio:
                _clip_propio(img, dur, g.tamano, clip, e.inicio_clip)
            else:
                _clip_escena(img, dur, e.idx, g.tamano, clip)
        clips.append(clip)

    ini = PORTADA_SEG if portada else 0.0
    fundidos = [XFADE] * (len(clips) - 1)
    duraciones_v = dur_clips
    if portada:
        decir("log.portada")
        clip = clips_dir / "portada.mp4"
        _clip_portada(portada, g.tamano, clip)
        clips, fundidos = [clip] + clips, [PORTADA_FUNDIDO] + fundidos
        duraciones_v = [ini - PORTADA_FUNDIDO] + dur_clips

    decir("log.uniendo")
    video_mudo = carpeta / "video_mudo.mp4"
    _unir_video(clips, duraciones_v, video_mudo, fundidos)

    decir("log.mezclando")
    audio = carpeta / "audio_final.m4a"
    voz_total = ini + (sum(duraciones) if limite is None else min(limite, sum(duraciones)))
    # el cierre va DESPUÉS de la última frase: la voz nunca se pisa, y la música sigue sonando encima
    total = duracion_prevista(g, voces, limite, cierre, bool(portada))
    if g.musica and g.musica in MOODS and not resolver(g.musica, g.slug):
        decir("log.musica_vacia", mood=g.musica)
    _unir_audio([carpeta / v["mp3"] for v in voces], duraciones, audio, resolver(g.musica, g.slug),
                g.volumen_musica, total, silencio_inicial=ini)

    decir("log.render")
    final = destino or carpeta / f"{g.slug}.mp4"
    # FFmpeg corre en carpeta: una ruta explícita se resuelve desde el llamador.
    salida_ffmpeg = str(Path(destino).resolve()) if destino is not None else final.name
    if destino is not None:
        Path(salida_ffmpeg).parent.mkdir(parents=True, exist_ok=True)
    w, h = g.tamano
    args = ["ffmpeg", "-y", "-v", "error", "-i", video_mudo.name, "-i", audio.name]
    filtros, v = [], "[0:v]"
    if subtitulos:
        filtros.append(f"{v}{filtro_subtitulos(ass, carpeta)}[sub]")
        v = "[sub]"
    if marca_agua:
        em = estilo_marca or marca.EstiloMarca()
        args += ["-i", str(marca.marca_agua(h, em.tamano, logo_marca))]
        # sobre la portada no: la miniatura ya trae su logo
        cuando = f":enable='gte(t,{ini:.3f})'" if ini else ""
        filtros.append(f"{v}[2:v]overlay={em.overlay(h)}{cuando}[marca]")
        v = "[marca]"
    if cierre:
        n = 2 + marca_agua
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{CIERRE_SEG}", "-i", str(tarjeta or marca.tarjeta_cierre((w, h)))]
        # la última imagen se congela CIERRE_FUNDIDO s para fundirse con la tarjeta sin recortar la escena
        filtros += [
            f"{v}settb=AVTB,fps={FPS},format=yuv420p,tpad=stop_mode=clone:stop_duration={CIERRE_FUNDIDO}[fin]",
            f"[{n}:v]settb=AVTB,fps={FPS},format=yuv420p[tarjeta]",
            f"[fin][tarjeta]xfade=transition=fade:duration={CIERRE_FUNDIDO}:offset={voz_total:.3f}[cierre]",
        ]
        v = "[cierre]"
    if prueba:
        n = 2 + marca_agua + cierre
        args += ["-i", str(marca.marca_prueba((w, h)))]
        filtros.append(f"{v}[{n}:v]overlay=0:0[prueba]")
        v = "[prueba]"
    filtros.append("[1:a]apad[a]")  # sin música, la voz se rellena con silencio hasta el final del cierre
    args += ["-filter_complex", ";".join(filtros), "-map", v, "-map", "[a]", "-t", f"{total:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", salida_ffmpeg]
    _run(args, cwd=carpeta)
    return final
