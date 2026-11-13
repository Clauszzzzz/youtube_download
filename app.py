from datetime import datetime, timezone
import importlib.util
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

st.set_page_config(page_title='YouTube Downloader — V19', page_icon='🎬', layout='wide')

CLIENTES = ['tv', 'ios', 'mweb', 'android_vr', 'web_embedded', 'android']
RESOLUCOES = [2160, 1440, 1080, 720, 480, 360, 240, 144]
MAX_BYTES = 200 * 1024 * 1024  # 200 MB evita estouro de RAM (OOM) no Streamlit Cloud


def redigir(texto: str) -> str:
    """Remove segredos e sanitiza URLs assinadas preservando parâmetros úteis de rede."""
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
    return texto[-25000:]


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


def run_cmd(cmd: list[str], timeout: int = 15) -> str:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        first_line = (p.stdout or '').strip().splitlines()[0] if (p.stdout or '').strip() else ''
        return f'OK (rc={p.returncode}): {first_line[:120]}'
    except Exception as e:
        return f'ERRO: {type(e).__name__}: {e}'


def detect_js_runtimes() -> dict[str, Any]:
    runtimes = {}
    deno = shutil.which('deno')
    node = shutil.which('node')
    if deno:
        runtimes['deno'] = {'path': deno}
    if node:
        runtimes['node'] = {'path': node}
    return runtimes


def get_environment_info(cookies_loaded: bool = False) -> dict[str, Any]:
    runtimes = detect_js_runtimes()
    return {
        'app_version': 'V19',
        'timestamp_utc': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'yt_dlp_version': yt_dlp.version.__version__,
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'curl_cffi_installed': bool(importlib.util.find_spec('curl_cffi')),
        'js_runtimes_detected': list(runtimes.keys()) or ['nenhum'],
        'deno_check': run_cmd(['deno', '--version']) if 'deno' in runtimes else 'não encontrado',
        'node_check': run_cmd(['node', '--version']) if 'node' in runtimes else 'não encontrado',
        'ffmpeg_check': run_cmd(['ffmpeg', '-version']) if shutil.which('ffmpeg') else 'não encontrado',
        'cookies_uploaded': cookies_loaded,
        'force_ipv4': True,
    }


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
            'logs': logs[-200:],
        }
    except Exception as e:
        return None, {
            'cliente': cliente,
            'status': 'ERRO',
            'error_type': type(e).__name__,
            'error': redigir(str(e)),
            'traceback': redigir(traceback.format_exc()),
            'logs': logs[-200:],
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
    result = {
        'cliente': cliente,
        'media': media,
        'quality': quality,
        'selector': selector,
        'status': 'ERRO',
        'error': None,
        'traceback': None,
        'logs': [],
    }
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
        result['traceback'] = redigir(traceback.format_exc())
    result['logs'] = logs[-400:]
    result['network_hints'] = download_url_network_hints(result['logs'])
    result['extension'] = ext
    return result


def build_diagnostic_report(cookies_loaded: bool) -> str:
    """Gera um relatório técnico legível em texto puro (.txt) para análise."""
    env = get_environment_info(cookies_loaded=cookies_loaded)
    lines: list[str] = [
        '====================================================================',
        'RELATÓRIO DE DIAGNÓSTICO — YOUTUBE DOWNLOADER (V19)',
        '====================================================================',
        '',
        '## 1. INFORMAÇÕES DA APLICAÇÃO E AMBIENTE',
    ]
    for k, v in env.items():
        lines.append(f'- {k}: {v}')

    lines.append(f"- url_informada: {st.session_state.get('url', 'N/A')}")
    lines.append(f"- cliente_selecionado: {st.session_state.get('selected_client', 'N/A')}")
    if st.session_state.get('fatal_error'):
        lines.append(f"- erro_geral: {st.session_state.get('fatal_error')}")

    results = st.session_state.get('client_results', [])
    lines.extend(['', '## 2. TESTE DE EXTRAÇÃO POR CLIENTE'])
    if not results:
        lines.append('Nenhuma análise de vídeo executada ainda.')
    else:
        for r in results:
            lines.append(f"\n### Cliente: {r.get('cliente')} | Status: {r.get('status')}")
            if r.get('status') == 'OK':
                lines.append(
                    f"Título: {r.get('title')} | Máxima: {r.get('max_height')}p | "
                    f"Formatos: {r.get('format_count')} (Vídeo: {r.get('video_format_count')}, Áudio: {r.get('audio_format_count')})"
                )
            else:
                lines.append(f"Tipo de Erro: {r.get('error_type')}")
                lines.append(f"Mensagem: {r.get('error')}")
                if r.get('traceback'):
                    lines.append('--- Traceback ---')
                    lines.append(r['traceback'])
            lines.append('--- Logs de Extração (últimas 80 linhas) ---')
            lines.extend((r.get('logs') or ['Sem logs.'])[-80:])

    attempts = st.session_state.get('download_attempts', [])
    lines.extend(['', '## 3. TENTATIVAS DE DOWNLOAD (FALLBACK)'])
    if not attempts:
        lines.append('Nenhuma tentativa de download executada ainda.')
    else:
        for idx, att in enumerate(attempts, 1):
            lines.append(
                f"\n### Tentativa #{idx} | Cliente: {att.get('cliente')} | "
                f"Mídia: {att.get('media')} ({att.get('quality')}) | Status: {att.get('status')}"
            )
            lines.append(f"Seletor: {att.get('selector')}")
            lines.append(f"Pistas de Rede: {att.get('network_hints')}")
            if att.get('error'):
                lines.append(f"Erro: {att['error']}")
            if att.get('traceback'):
                lines.append('--- Traceback ---')
                lines.append(att['traceback'])
            lines.append('--- Logs Reais do Download ---')
            lines.extend(att.get('logs') or ['Sem logs.'])

    lines.extend(['', '====================================================================', 'FIM DO RELATÓRIO'])
    return '\n'.join(lines)


def main():
    st.title('🎬 YouTube Downloader — V19')
    st.caption('Com fallback automático de clientes, suporte a cookies.txt e exportação de relatório em .txt.')

    with st.expander('Ambiente e Cookies (opcional para evitar Erro 403 na nuvem)', expanded=False):
        cookies_upload = st.file_uploader(
            'Arquivo cookies.txt (formato Netscape) — use se o servidor receber bloqueio 403',
            type=['txt'],
        )
        st.json(get_environment_info(cookies_loaded=cookies_upload is not None))

    url = st.text_input(
        'URL do YouTube',
        value=st.session_state.get('url', ''),
        placeholder='https://www.youtube.com/watch?v=...',
    )
    if url:
        st.session_state['url'] = url

    c1, c2 = st.columns([3, 1])
    with c1:
        analyze = st.button('🔎 Analisar vídeo', type='primary', use_container_width=True)
    with c2:
        diag_btn = st.button('🧪 Diagnóstico completo', use_container_width=True)

    cookie_path = None
    if cookies_upload is not None:
        tmp_cookie = tempfile.NamedTemporaryFile(delete=False, suffix='.txt')
        tmp_cookie.write(cookies_upload.getvalue())
        tmp_cookie.close()
        cookie_path = tmp_cookie.name

    try:
        if analyze or diag_btn:
            st.session_state.pop('ready_file', None)
            st.session_state.pop('download_attempts', None)
            st.session_state.pop('fatal_error', None)
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
                    st.error('Nenhum cliente conseguiu extrair informações do vídeo. Baixe o relatório abaixo e envie para análise.')
            except Exception as e:
                st.session_state['fatal_error'] = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
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
        if info:
            st.divider()
            st.subheader(info.get('title') or 'Vídeo')
            st.write(f"**Duração:** {format_duration(info.get('duration'))}")

            formats = info.get('formats') or []
            heights = sorted(
                {int(f['height']) for f in formats if f.get('height') and f.get('vcodec') not in (None, 'none')},
                reverse=True,
            )
            options = available_options(max(heights, default=0))

            media_label = st.radio('Tipo de mídia', ['Vídeo MP4 (Com Áudio)', 'Apenas Áudio MP3'], horizontal=True)
            if media_label.startswith('Vídeo'):
                if not options:
                    st.warning('Nenhum formato de vídeo encontrado.')
                else:
                    quality_label = st.selectbox('Qualidade', list(options))
                    quality = options[quality_label]
                    media = 'video'
            else:
                audio_q = st.selectbox('Qualidade do áudio', ['320 kbps', '192 kbps', '128 kbps'])
                quality = int(audio_q.split()[0])
                media = 'audio'

            if (media_label.startswith('Apenas') or options) and st.button('⬇️ Baixar arquivo', type='primary', use_container_width=True):
                st.session_state.pop('ready_file', None)
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

                attempts = []
                success = False

                for client in working_clients:
                    status.info(f'Tentando download via cliente `{client}`...')
                    with tempfile.TemporaryDirectory(prefix='yt_v19_') as folder:
                        result = download_one(
                            normalize_url(url), client, media, quality, folder, cookiefile=cookie_path, hook=hook
                        )
                        attempts.append(result)
                        st.session_state['download_attempts'] = attempts

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

                if not success and attempts:
                    last_err = attempts[-1].get('error') or {}
                    st.error(last_err.get('message') or 'Todos os clientes falharam no download.')

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

        # Botão sempre visível após qualquer análise/tentativa para exportar o relatório completo em .txt
        if st.session_state.get('client_results') or st.session_state.get('download_attempts') or st.session_state.get('fatal_error'):
            st.divider()
            st.subheader('📋 Relatório de Diagnóstico')
            st.caption('Se algo falhar, baixe este arquivo .txt e anexe aqui no chat para eu analisar exatamente o que ocorreu.')
            report_text = build_diagnostic_report(cookies_loaded=cookies_upload is not None)
            st.download_button(
                '📄 Baixar relatório de erro (.txt)',
                report_text.encode('utf-8'),
                'relatorio_yt_downloader_v19.txt',
                'text/plain',
                use_container_width=True,
                key='download_txt_report_v19',
            )
    finally:
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)


if __name__ == '__main__':
    main()
