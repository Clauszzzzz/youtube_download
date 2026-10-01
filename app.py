import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional

import streamlit as st
import yt_dlp

st.set_page_config(page_title='YouTube Downloader — Diagnóstico V16', page_icon='🎬', layout='wide')

CLIENTES = ['web_embedded', 'android', 'android_vr']
RESOLUCOES = [2160, 1440, 1080, 720, 480, 360, 240, 144]
MAX_BYTES = 500 * 1024 * 1024


def redigir(texto: str) -> str:
    texto = str(texto)
    patterns = [
        (r'(?i)(authorization\s*[:=]\s*)\S+', r'\1[OCULTO]'),
        (r'(?i)(cookie\s*[:=]\s*)\S+', r'\1[OCULTO]'),
        (r'(?i)(po.?token[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
        (r'(?i)(visitor[-_ ]data[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
        (r'(?i)(data[-_ ]sync[-_ ]id[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
    ]
    for pattern, repl in patterns:
        texto = re.sub(pattern, repl, texto)
    return texto[-16000:]


def run_cmd(cmd: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return {'returncode': p.returncode, 'output': redigir(p.stdout or '')[-6000:]}
    except Exception as e:
        return {'returncode': None, 'output': f'{type(e).__name__}: {e}'}


def js_runtime() -> dict[str, Any]:
    deno = shutil.which('deno')
    if not deno:
        raise RuntimeError('Deno não foi encontrado. Verifique requirements.txt.')
    return {'deno': deno, 'js_runtimes': {'deno': {'path': deno}}}


def yt_opts(cliente: str, download: bool = False, **extra: Any) -> dict[str, Any]:
    env = js_runtime()
    opts = {
        'noplaylist': True,
        'quiet': not download,
        'no_warnings': False,
        'verbose': True,
        'socket_timeout': 30,
        'retries': 2,
        'fragment_retries': 2,
        'js_runtimes': env['js_runtimes'],
        'extractor_args': {'youtube': {'player_client': [cliente]}},
        **extra,
    }
    return opts


def extract(url: str, cliente: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    logs: list[str] = []

    class Logger:
        def debug(self, msg): logs.append(redigir(msg))
        def info(self, msg): logs.append(redigir(msg))
        def warning(self, msg): logs.append('WARNING: ' + redigir(msg))
        def error(self, msg): logs.append('ERROR: ' + redigir(msg))

    try:
        opts = yt_opts(cliente, logger=Logger(), skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = info.get('formats') or []
        heights = sorted({int(f['height']) for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')}, reverse=True)
        video_formats = [f for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')]
        audio_formats = [f for f in formats if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')]
        return info, {
            'cliente': cliente,
            'status': 'OK',
            'title': info.get('title'),
            'id': info.get('id'),
            'max_height': max(heights, default=0),
            'heights': heights,
            'format_count': len(formats),
            'video_format_count': len(video_formats),
            'audio_format_count': len(audio_formats),
            'logs': logs[-250:],
        }
    except Exception as e:
        return None, {
            'cliente': cliente,
            'status': 'ERRO',
            'error_type': type(e).__name__,
            'error': redigir(str(e)),
            'traceback': redigir(traceback.format_exc()),
            'logs': logs[-250:],
        }


def normalize_url(url: str) -> str:
    url = url.strip()
    if 'youtube.com/playlist' in url:
        raise ValueError('Playlists não são suportadas; use um vídeo individual.')
    if 'youtube.com/watch' in url and '&list=' in url:
        url = url.split('&list=', 1)[0]
    return url


def format_duration(v):
    if not v:
        return 'N/A'
    v = int(v)
    h, r = divmod(v, 3600)
    m, s = divmod(r, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def format_bytes(v):
    if v is None:
        return 'N/A'
    v = float(v)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if v < 1024:
            return f'{v:.1f} {unit}'
        v /= 1024
    return f'{v:.1f} TB'


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name or 'download')
    return re.sub(r'\s+', ' ', name).strip().rstrip('. ')[:180] or 'download'


def available_options(height: int) -> dict[str, int]:
    if height <= 0:
        return {}
    usable = [x for x in RESOLUCOES if x <= height]
    if not usable:
        return {f'{height}p (máxima disponível)': height}
    return {f'{x}p' + (' (máxima disponível)' if i == 0 else ''): x for i, x in enumerate(usable)}


def download_one(url: str, cliente: str, media: str, quality: int, folder: str, hook=None) -> dict[str, Any]:
    logs: list[str] = []
    class Logger:
        def debug(self, msg): logs.append(redigir(msg))
        def info(self, msg): logs.append(redigir(msg))
        def warning(self, msg): logs.append('WARNING: ' + redigir(msg))
        def error(self, msg): logs.append('ERROR: ' + redigir(msg))

    if media == 'video':
        selector = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        extra = {'format': selector, 'merge_output_format': 'mp4'}
        ext = '.mp4'
    else:
        selector = 'bestaudio/best'
        extra = {'format': selector, 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': str(quality)}]}
        ext = '.mp3'

    opts = yt_opts(
        cliente,
        True,
        logger=Logger(),
        outtmpl=str(Path(folder) / '%(title).180s.%(ext)s'),
        progress_hooks=[hook] if hook else [],
        **extra,
    )
    result = {'cliente': cliente, 'selector': selector, 'status': 'ERRO', 'error': None, 'logs': []}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            rc = ydl.download([url])
        result['status'] = 'OK' if rc == 0 else f'RETORNO_{rc}'
    except Exception as e:
        result['error'] = {'type': type(e).__name__, 'message': redigir(str(e)), 'is_403': '403' in str(e) or 'Forbidden' in str(e)}
    result['logs'] = logs[-350:]
    result['extension'] = ext
    return result


def main():
    st.title('🎬 YouTube Downloader — Diagnóstico Completo V16')
    st.caption('Versão limpa: sem BgUtils na inicialização. O objetivo é separar cliente, formatos e HTTP 403.')
    st.info('Use somente conteúdo que você tenha autorização para baixar ou que seja permitido pelos termos e direitos aplicáveis.')

    with st.expander('Ambiente', expanded=False):
        st.json({
            'yt_dlp': yt_dlp.version.__version__,
            'python': platform.python_version(),
            'platform': platform.platform(),
            'deno': run_cmd(['deno', '--version']) if shutil.which('deno') else 'não encontrado',
            'ffmpeg': run_cmd(['ffmpeg', '-version']) if shutil.which('ffmpeg') else 'não encontrado',
        })

    url = st.text_input('URL do YouTube', value=st.session_state.get('url', ''), placeholder='https://www.youtube.com/watch?v=...')
    if url:
        st.session_state['url'] = url

    c1, c2 = st.columns([3, 1])
    with c1:
        analyze = st.button('🔎 Analisar vídeo', type='primary', use_container_width=True)
    with c2:
        diag_btn = st.button('🧪 Diagnóstico completo', use_container_width=True)

    if analyze or diag_btn:
        try:
            u = normalize_url(url)
            results = []
            infos = []
            for client in CLIENTES:
                with st.spinner(f'Testando {client}...'):
                    info, result = extract(u, client)
                results.append(result)
                if info:
                    infos.append((result['max_height'], client, info))
            st.session_state['client_results'] = results
            st.session_state['client_infos'] = infos
            if infos:
                infos.sort(key=lambda x: x[0], reverse=True)
                st.session_state['video_info'] = infos[0][2]
                st.session_state['selected_client'] = infos[0][1]
            else:
                st.session_state.pop('video_info', None)
        except Exception as e:
            st.error(str(e))

    results = st.session_state.get('client_results', [])
    if results:
        st.subheader('Resultado por cliente')
        rows = []
        for r in results:
            rows.append({
                'Cliente': r.get('cliente'),
                'Status': r.get('status'),
                'Máxima': f"{r.get('max_height', 0)}p" if r.get('status') == 'OK' else '-',
                'Formatos': r.get('format_count', '-'),
                'Vídeo': r.get('video_format_count', '-'),
                'Áudio': r.get('audio_format_count', '-'),
                'Erro': r.get('error', '')[:220] if r.get('status') != 'OK' else '',
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        for r in results:
            with st.expander(f"Detalhes — {r.get('cliente')}"):
                st.json({k: v for k, v in r.items() if k not in ('logs', 'traceback')})
                if r.get('traceback'):
                    st.code(r['traceback'], language='text')
                st.code('\n'.join(r.get('logs') or ['Sem logs.']), language='text')

        if diag_btn:
            payload = {
                'app': 'V16 clean',
                'yt_dlp': yt_dlp.version.__version__,
                'python': platform.python_version(),
                'platform': platform.platform(),
                'url': normalize_url(url),
                'results': results,
            }
            st.download_button('📄 Baixar diagnóstico JSON', json.dumps(payload, ensure_ascii=False, indent=2).encode(), 'diagnostico_v16.json', 'application/json', use_container_width=True)

    info = st.session_state.get('video_info')
    if not info:
        return

    st.divider()
    st.subheader(info.get('title') or 'Vídeo')
    st.write(f"**Duração:** {format_duration(info.get('duration'))}")
    st.write(f"**Cliente que forneceu a maior lista:** `{st.session_state.get('selected_client')}`")

    formats = info.get('formats') or []
    heights = sorted({int(f['height']) for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')}, reverse=True)
    options = available_options(max(heights, default=0))
    if not options:
        st.warning('Nenhum formato de vídeo foi retornado pelo cliente selecionado.')
        return

    media_label = st.radio('Tipo de mídia', ['Vídeo MP4 (Com Áudio)', 'Apenas Áudio MP3'], horizontal=True)
    if media_label.startswith('Vídeo'):
        quality_label = st.selectbox('Qualidade', list(options))
        quality = options[quality_label]
        media = 'video'
        st.caption(f'Máxima encontrada na resposta selecionada: **{max(heights)}p**.')
    else:
        audio_q = st.selectbox('Qualidade do áudio', ['320 kbps', '192 kbps', '128 kbps'])
        quality = int(audio_q.split()[0])
        media = 'audio'

    if st.button('⬇️ Baixar arquivo', type='primary', use_container_width=True):
        client = st.session_state.get('selected_client')
        with tempfile.TemporaryDirectory(prefix='yt_v16_') as folder:
            bar = st.progress(0)
            status = st.empty()
            def hook(data):
                if data.get('status') == 'downloading':
                    total = data.get('total_bytes') or data.get('total_bytes_estimate')
                    got = data.get('downloaded_bytes', 0)
                    if total:
                        p = min(max(got / total, 0), 1)
                        bar.progress(int(p * 100))
                        status.info(f'Baixando: {p*100:.1f}%')
                elif data.get('status') == 'finished':
                    bar.progress(100)
                    status.info('Download do fluxo concluído; finalizando...')
            result = download_one(normalize_url(url), client, media, quality, folder, hook)
            st.session_state['last_download'] = result
            if result['status'] == 'OK':
                files = [p for p in Path(folder).iterdir() if p.is_file()]
                if not files:
                    st.error('O yt-dlp informou sucesso, mas nenhum arquivo foi encontrado.')
                else:
                    final = max(files, key=lambda p: p.stat().st_mtime)
                    if final.stat().st_size > MAX_BYTES:
                        st.error('O arquivo final ultrapassou 500 MB.')
                    else:
                        st.success(f'Arquivo pronto — {format_bytes(final.stat().st_size)}')
                        st.download_button('💾 Salvar no computador', final.read_bytes(), safe_filename(info.get('title')) + ('.mp4' if media == 'video' else '.mp3'), 'video/mp4' if media == 'video' else 'audio/mpeg', use_container_width=True)
            else:
                st.error(result.get('error', {}).get('message') or 'Falha no download.')
                with st.expander('Log real do download', expanded=True):
                    st.json({k: v for k, v in result.items() if k != 'logs'})
                    st.code('\n'.join(result.get('logs') or ['Sem logs.']), language='text')

if __name__ == '__main__':
    main()
