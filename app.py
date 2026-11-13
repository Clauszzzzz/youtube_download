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
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import streamlit as st
import yt_dlp

st.set_page_config(page_title='YouTube Downloader — V18 Corrigido', page_icon='🎬', layout='wide')

# Clientes ordenados dos mais compatíveis sem PO Token para os mais restritos
CLIENTES = ['tv', 'ios', 'mweb', 'android_vr', 'web_embedded', 'android']
RESOLUCOES = [2160, 1440, 1080, 720, 480, 360, 240, 144]
MAX_BYTES = 200 * 1024 * 1024  # 200 MB evita estouro de RAM (OOM) no Streamlit Cloud


def redigir(texto: str) -> str:
    texto = str(texto)

    def sanitize_googlevideo(match):
        raw = match.group(0)
        try:
            u = urlparse(raw)
            q = parse_qs(u.query)
            keep = {k: q[k][0] for k in ('ip', 'itag', 'c', 'mime') if k in q and q[k]}
            query = '&'.join(f'{k}={v}' for k, v in keep.items())
            return f'{u.scheme}://{u.netloc}{u.path}' + (f'?{query}' if query else '?[PARAMETROS_OCULTOS]')
        except Exception:
            return '[URL_GOOGLEVIDEO_OCULTA]'

    texto = re.sub(r'https?://[^\s"\']*googlevideo\.com[^\s"\']*', sanitize_googlevideo, texto, flags=re.I)

    patterns = [
        (r'(?i)(authorization\s*[:=]\s*)\S+', r'\1[OCULTO]'),
        (r'(?i)(cookie\s*[:=]\s*)\S+', r'\1[OCULTO]'),
        (r'(?i)(po.?token[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
        (r'(?i)(visitor[-_ ]data[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
        (r'(?i)(data[-_ ]sync[-_ ]id[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
    ]
    for pattern, repl in patterns:
        texto = re.sub(pattern, repl, texto)
    return texto[-20000:]


def download_url_network_hints(logs: list[str]) -> dict[str, Any]:
    joined = '\n'.join(logs)
    m = re.search(r'https?://[^\s"\']*googlevideo\.com[^\s"\']*', joined, flags=re.I)
    if not m:
        return {'googlevideo_url_found': False}
    raw = m.group(0)
    try:
        u = urlparse(raw)
        q = parse_qs(u.query)
        ip = q.get('ip', [None])[0]
        family = 'IPv6' if ip and ':' in ip else ('IPv4' if ip else 'desconhecida')
        return {
            'googlevideo_url_found': True,
            'host': u.netloc,
            'ip_parameter_present': bool(ip),
            'ip_family_in_url': family,
            'ip_parameter': ip,
            'itag': q.get('itag', [None])[0],
            'client_parameter_c': q.get('c', [None])[0],
        }
    except Exception as e:
        return {'googlevideo_url_found': True, 'parse_error': f'{type(e).__name__}: {e}'}


def run_cmd(cmd: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return {'returncode': p.returncode, 'output': redigir(p.stdout or '')[-6000:]}
    except Exception as e:
        return {'returncode': None, 'output': f'{type(e).__name__}: {e}'}


def detect_js_runtimes() -> dict[str, Any]:
    """Detecta runtimes JS disponíveis sem quebrar o app caso não existam."""
    runtimes = {}
    deno = shutil.which('deno')
    node = shutil.which('node')
    if deno:
        runtimes['deno'] = {'path': deno}
    if node:
        runtimes['node'] = {'path': node}
    return runtimes


def yt_opts(cliente: str, download: bool = False, cookiefile: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'noplaylist': True,
        'quiet': not download,
        'no_warnings': False,
        'verbose': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'source_address': '0.0.0.0',
        'extractor_args': {'youtube': {'player_client': [cliente]}},
        **extra,
    }
    runtimes = detect_js_runtimes()
    if runtimes:
        opts['js_runtimes'] = runtimes
    if cookiefile:
        opts['cookiefile'] = cookiefile
    return opts


def extract(url: str, cliente: str, cookiefile: Optional[str] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    logs: list[str] = []

    class Logger:
        def debug(self, msg): logs.append(redigir(msg))
        def info(self, msg): logs.append(redigir(msg))
        def warning(self, msg): logs.append('WARNING: ' + redigir(msg))
        def error(self, msg): logs.append('ERROR: ' + redigir(msg))

    try:
        opts = yt_opts(cliente, cookiefile=cookiefile, logger=Logger(), skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = info.get('formats') or []
        heights = sorted(
            {int(f['height']) for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')},
            reverse=True,
        )
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
    if not url:
        raise ValueError('Informe uma URL válida.')
    if 'youtube.com/playlist' in url:
        raise ValueError('Playlists não são suportadas; use um vídeo individual.')
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for param in ('list', 'index', 'start_radio'):
        qs.pop(param, None)
    clean_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=clean_query))


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


def download_one(
    url: str,
    cliente: str,
    media: str,
    quality: int,
    folder: str,
    cookiefile: Optional[str] = None,
    hook=None,
) -> dict[str, Any]:
    logs: list[str] = []

    class Logger:
        def debug(self, msg): logs.append(redigir(msg))
        def info(self, msg): logs.append(redigir(msg))
        def warning(self, msg): logs.append('WARNING: ' + redigir(msg))
        def error(self, msg): logs.append('ERROR: ' + redigir(msg))

    if media == 'video':
        selector = f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        extra = {'format': selector, 'merge_output_format': 'mp4'}
        ext = '.mp4'
    else:
        selector = 'bestaudio/best'
        extra = {
            'format': selector,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': str(quality),
            }],
        }
        ext = '.mp3'

    opts = yt_opts(
        cliente,
        download=True,
        cookiefile=cookiefile,
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
        result['error'] = {
            'type': type(e).__name__,
            'message': redigir(str(e)),
            'is_403': '403' in str(e) or 'Forbidden' in str(e),
        }
    result['logs'] = logs[-500:]
    result['network_hints'] = download_url_network_hints(result['logs'])
    result['extension'] = ext
    return result


def main():
    st.title('🎬 YouTube Downloader — V18')
    st.caption('Com fallback automático de clientes, suporte a cookies.txt e persistência de arquivo.')

    with st.expander('Ambiente e Cookies (opcional para evitar Erro 403 na nuvem)', expanded=False):
        st.json({
            'yt_dlp': yt_dlp.version.__version__,
            'python': platform.python_version(),
            'platform': platform.platform(),
            'js_runtimes': list(detect_js_runtimes().keys()) or ['nenhum (opcional)'],
            'ffmpeg': 'disponível' if shutil.which('ffmpeg') else 'não encontrado',
        })
        cookies_upload = st.file_uploader(
            'Arquivo cookies.txt (Netscape format) — necessário se o servidor receber bloqueio 403 do YouTube',
            type=['txt'],
        )

    url = st.text_input('URL do YouTube', value=st.session_state.get('url', ''), placeholder='https://www.youtube.com/watch?v=...')
    if url:
        st.session_state['url'] = url

    c1, c2 = st.columns([3, 1])
    with c1:
        analyze = st.button('🔎 Analisar vídeo', type='primary', use_container_width=True)
    with c2:
        diag_btn = st.button('🧪 Diagnóstico completo', use_container_width=True)

    # Salva cookies temporariamente se enviados
    cookie_path = None
    if cookies_upload is not None:
        tmp_cookie = tempfile.NamedTemporaryFile(delete=False, suffix='.txt')
        tmp_cookie.write(cookies_upload.getvalue())
        tmp_cookie.close()
        cookie_path = tmp_cookie.name

    try:
        if analyze or diag_btn:
            st.session_state.pop('ready_file', None)
            try:
                u = normalize_url(url)
                results = []
                infos = []
                for client in CLIENTES:
                    with st.spinner(f'Testando cliente {client}...'):
                        info, result = extract(u, client, cookiefile=cookie_path)
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
            rows = [{
                'Cliente': r.get('cliente'),
                'Status': r.get('status'),
                'Máxima': f"{r.get('max_height', 0)}p" if r.get('status') == 'OK' else '-',
                'Formatos': r.get('format_count', '-'),
                'Erro': r.get('error', '')[:180] if r.get('status') != 'OK' else '',
            } for r in results]
            st.dataframe(rows, use_container_width=True, hide_index=True)

        info = st.session_state.get('video_info')
        if not info:
            return

        st.divider()
        st.subheader(info.get('title') or 'Vídeo')
        st.write(f"**Duração:** {format_duration(info.get('duration'))}")

        formats = info.get('formats') or []
        heights = sorted({int(f['height']) for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')}, reverse=True)
        options = available_options(max(heights, default=0))

        media_label = st.radio('Tipo de mídia', ['Vídeo MP4 (Com Áudio)', 'Apenas Áudio MP3'], horizontal=True)
        if media_label.startswith('Vídeo'):
            if not options:
                st.warning('Nenhum formato de vídeo encontrado.')
                return
            quality_label = st.selectbox('Qualidade', list(options))
            quality = options[quality_label]
            media = 'video'
        else:
            audio_q = st.selectbox('Qualidade do áudio', ['320 kbps', '192 kbps', '128 kbps'])
            quality = int(audio_q.split()[0])
            media = 'audio'

        if st.button('⬇️ Baixar arquivo', type='primary', use_container_width=True):
            # Monta lista de clientes que funcionaram na análise para tentar com fallback
            working_clients = [c for _, c, _ in st.session_state.get('client_infos', [])] or CLIENTES
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
                    status.info('Convertendo/mesclando mídia com FFmpeg...')

            success = False
            last_result = None

            for client in working_clients:
                status.info(f'Tentando download via cliente `{client}`...')
                with tempfile.TemporaryDirectory(prefix='yt_v18_') as folder:
                    result = download_one(
                        normalize_url(url), client, media, quality, folder, cookiefile=cookie_path, hook=hook
                    )
                    last_result = result
                    st.session_state['last_download'] = result

                    if result['status'] == 'OK':
                        files = [p for p in Path(folder).iterdir() if p.is_file()]
                        if files:
                            final = max(files, key=lambda p: p.stat().st_mtime)
                            if final.stat().st_size > MAX_BYTES:
                                st.error('O arquivo ultrapassou 200 MB (limite seguro de memória na nuvem). Escolha uma resolução menor.')
                                break
                            st.session_state['ready_file'] = {
                                'bytes': final.read_bytes(),
                                'name': safe_filename(info.get('title')) + ('.mp4' if media == 'video' else '.mp3'),
                                'mime': 'video/mp4' if media == 'video' else 'audio/mpeg',
                                'size': format_bytes(final.stat().st_size),
                                'client': client,
                            }
                            success = True
                            break

            if not success and last_result:
                st.error(last_result.get('error', {}).get('message') or 'Todos os clientes falharam no download.')
                with st.expander('Log do último erro', expanded=True):
                    st.code('\n'.join(last_result.get('logs') or ['Sem logs.']), language='text')

        # Renderiza o botão de salvar FORA do bloco do botão de baixar para não sumir no rerun
        ready = st.session_state.get('ready_file')
        if ready:
            st.success(f"Arquivo pronto ({ready['size']}) via `{ready['client']}`!")
            st.download_button(
                '💾 Salvar no computador',
                ready['bytes'],
                ready['name'],
                ready['mime'],
                use_container_width=True,
                key='save_final_file',
            )
    finally:
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)


if __name__ == '__main__':
    main()
