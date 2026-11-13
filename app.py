import base64
import os
import re
import shutil
import tempfile
from pathlib import Path

import streamlit as st
import yt_dlp

APP_VERSION = "v1.0"

st.set_page_config(
    page_title="YouTube Downloader",
    page_icon="🎬",
    layout="centered",
)


def is_local_windows() -> bool:
    return os.name == "nt"


def get_downloads_dir() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return downloads
    return Path.cwd()


def detect_deno_path() -> str | None:
    found = shutil.which("deno")
    if found:
        return found
    for candidate in (
        Path.home() / ".deno" / "bin" / "deno.exe",
        Path("/home/adminuser/venv/bin/deno"),
        Path.home() / ".deno" / "bin" / "deno",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def get_cookie_file() -> str | None:
    uploaded = st.session_state.get("uploaded_cookies_bytes")
    if uploaded:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="wb")
        tmp.write(uploaded)
        tmp.close()
        return tmp.name

    try:
        if "YOUTUBE_COOKIES" in st.secrets and str(st.secrets["YOUTUBE_COOKIES"]).strip():
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
            tmp.write(str(st.secrets["YOUTUBE_COOKIES"]).strip() + "\n")
            tmp.close()
            return tmp.name
    except Exception:
        pass

    return None


def apply_custom_background():
    bg_data_uri = None
    bg_file = st.session_state.get("bg_file_bytes")
    bg_mime = st.session_state.get("bg_file_mime", "image/jpeg")
    bg_url = (st.session_state.get("bg_image_url") or "").strip()
    overlay_opacity = st.session_state.get("bg_overlay_opacity", 0.72)

    if bg_file:
        b64 = base64.b64encode(bg_file).decode("utf-8")
        bg_data_uri = f"data:{bg_mime};base64,{b64}"
    elif bg_url:
        bg_data_uri = bg_url

    if not bg_data_uri:
        return

    st.markdown(
        f"""
        <style>
        .stApp {{
            background-image: linear-gradient(
                rgba(10, 12, 18, {overlay_opacity}),
                rgba(10, 12, 18, {overlay_opacity})
            ), url("{bg_data_uri}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
        }}
        [data-testid="stHeader"] {{
            background: rgba(0, 0, 0, 0);
        }}
        .block-container {{
            background: rgba(18, 22, 31, 0.78);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 2rem 2rem 2.5rem 2rem;
            margin-top: 1.5rem;
            border: 1px solid rgba(255, 255, 255, 0.08);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned[:160] or "video_youtube"


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Desconhecida"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_base_ydl_opts(client: str, cookiefile: str | None) -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": [client]}},
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }
    deno_path = detect_deno_path()
    if deno_path:
        opts["js_runtimes"] = {"deno": {"path": deno_path}}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def analyze_video(url: str) -> tuple[dict, str]:
    cookiefile = get_cookie_file()
    clients = ["web_embedded", "mweb", "android_vr"] if not cookiefile else ["web_embedded", "mweb"]
    last_error = None

    try:
        for client in clients:
            opts = build_base_ydl_opts(client, cookiefile)
            opts["skip_download"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and info.get("formats"):
                    return info, client
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Não foi possível extrair os formatos do vídeo. Detalhe: {last_error}")
    finally:
        if cookiefile and os.path.exists(cookiefile):
            try:
                os.remove(cookiefile)
            except OSError:
                pass


def get_available_resolutions(info: dict) -> list[int]:
    heights = set()
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none") and fmt.get("height"):
            h = int(fmt["height"])
            if h >= 144:
                heights.add(h)
    standard = [2160, 1440, 1080, 720, 480, 360]
    filtered = [h for h in standard if h in heights]
    if not filtered and heights:
        filtered = sorted(heights, reverse=True)
    return filtered or [1080, 720, 480, 360]


def download_media(
    url: str,
    mode: str,
    resolution: int,
    audio_bitrate: str,
    preferred_client: str,
    progress_callback,
) -> Path:
    cookiefile = get_cookie_file()
    local_mode = is_local_windows()
    output_dir = get_downloads_dir() if local_mode else Path(tempfile.mkdtemp(prefix="yt_dl_"))

    if mode == "Vídeo (MP4)":
        if local_mode:
            # Localmente (IP residencial): prioriza DASH de alta resolução (4K/2K/1080p) antes do HLS
            selectors = [
                f"bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]/best",
                f"bestvideo[height<={resolution}][protocol*=m3u8]+bestaudio[protocol*=m3u8]/best[height<={resolution}][protocol*=m3u8]",
            ]
        else:
            # Nuvem: prioriza HLS para evitar HTTP 403 em IP de datacenter
            selectors = [
                f"bestvideo[height<={resolution}][protocol*=m3u8]+bestaudio[protocol*=m3u8]/best[height<={resolution}][protocol*=m3u8]/bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]/best",
            ]
    else:
        selectors = [
            "bestaudio/best",
            "bestaudio[protocol*=m3u8]/best[protocol*=m3u8]",
        ]

    clients_to_try = [preferred_client] + [c for c in ["web_embedded", "mweb"] if c != preferred_client]
    last_error = None

    try:
        for client in clients_to_try:
            for fmt_selector in selectors:
                before_files = set(output_dir.iterdir())
                opts = build_base_ydl_opts(client, cookiefile)
                opts["format"] = fmt_selector
                opts["outtmpl"] = str(output_dir / "%(title).160s.%(ext)s")
                opts["progress_hooks"] = [progress_callback]

                if mode == "Vídeo (MP4)":
                    opts["merge_output_format"] = "mp4"
                else:
                    opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": audio_bitrate,
                        }
                    ]

                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        prepared = Path(ydl.prepare_filename(info))
                        expected = prepared.with_suffix(".mp4" if mode == "Vídeo (MP4)" else ".mp3")
                        if expected.exists() and expected.stat().st_size > 0:
                            return expected

                    after_files = set(output_dir.iterdir()) - before_files
                    valid_new = [f for f in after_files if f.is_file() and f.stat().st_size > 0]
                    if valid_new:
                        return max(valid_new, key=lambda p: p.stat().st_mtime)
                except Exception as exc:
                    last_error = exc

        raise RuntimeError(f"Falha ao concluir o download. Detalhe: {last_error}")
    finally:
        if cookiefile and os.path.exists(cookiefile):
            try:
                os.remove(cookiefile)
            except OSError:
                pass


# Aplica plano de fundo personalizado (se configurado)
apply_custom_background()

st.title("🎬 YouTube Downloader")
st.caption(
    f"Versão {APP_VERSION} • "
    + ("Modo Local (4K liberado • Salva direto em Downloads)" if is_local_windows() else "Modo Nuvem")
)

with st.expander("⚙️ Personalizar Fundo e Opções Avançadas"):
    st.markdown("**🖼️ Imagem de Fundo**")
    col_bg1, col_bg2 = st.columns(2)
    with col_bg1:
        bg_upload = st.file_uploader(
            "Enviar imagem do computador",
            type=["jpg", "jpeg", "png", "webp"],
            key="bg_uploader_widget",
        )
        if bg_upload is not None:
            st.session_state["bg_file_bytes"] = bg_upload.getvalue()
            st.session_state["bg_file_mime"] = bg_upload.type or "image/jpeg"
    with col_bg2:
        bg_url_input = st.text_input(
            "Ou cole o link direto de uma imagem",
            value=st.session_state.get("bg_image_url", ""),
            placeholder="https://exemplo.com/imagem.jpg",
        )
        if bg_url_input != st.session_state.get("bg_image_url", ""):
            st.session_state["bg_image_url"] = bg_url_input

    col_op, col_clr = st.columns([3, 1])
    with col_op:
        opacity = st.slider(
            "Escurecimento do fundo (para leitura)",
            min_value=0.20,
            max_value=0.92,
            value=st.session_state.get("bg_overlay_opacity", 0.72),
            step=0.04,
        )
        if opacity != st.session_state.get("bg_overlay_opacity", 0.72):
            st.session_state["bg_overlay_opacity"] = opacity
            st.rerun()
    with col_clr:
        st.write("")
        if st.button("Limpar fundo", use_container_width=True):
            st.session_state.pop("bg_file_bytes", None)
            st.session_state.pop("bg_file_mime", None)
            st.session_state["bg_image_url"] = ""
            st.rerun()

    st.divider()
    st.markdown("**🍪 Cookies do YouTube (opcional no PC / necessário na nuvem)**")
    cookies_upload = st.file_uploader(
        "Anexar cookies.txt (formato Netscape)",
        type=["txt"],
        key="cookies_uploader_widget",
    )
    if cookies_upload is not None:
        st.session_state["uploaded_cookies_bytes"] = cookies_upload.getvalue()
        st.success("Arquivo cookies.txt carregado na sessão.")

url = st.text_input(
    "Cole o link do vídeo do YouTube:",
    placeholder="https://www.youtube.com/watch?v=...",
)

if st.button("🔎 Analisar vídeo", type="primary", use_container_width=True):
    if not url.strip():
        st.warning("Cole um link válido do YouTube primeiro.")
    else:
        with st.spinner("Buscando informações e qualidades disponíveis..."):
            try:
                info, client_used = analyze_video(url.strip())
                st.session_state["video_info"] = info
                st.session_state["video_url"] = url.strip()
                st.session_state["preferred_client"] = client_used
            except Exception as err:
                st.error(str(err))

if "video_info" in st.session_state:
    info = st.session_state["video_info"]
    st.divider()

    col_thumb, col_meta = st.columns([1.1, 1.5])
    with col_thumb:
        if info.get("thumbnail"):
            st.image(info["thumbnail"], use_container_width=True)
    with col_meta:
        st.subheader(info.get("title", "Vídeo sem título"))
        st.write(f"**Canal:** {info.get('uploader') or info.get('channel') or 'Desconhecido'}")
        st.write(f"**Duração:** {format_duration(info.get('duration'))}")

    st.write("")
    mode = st.radio(
        "Formato desejado:",
        ["Vídeo (MP4)", "Áudio (MP3)"],
        horizontal=True,
    )

    available_res = get_available_resolutions(info)
    if mode == "Vídeo (MP4)":
        res_labels = {
            2160: "2160p (4K Ultra HD)",
            1440: "1440p (2K Quad HD)",
            1080: "1080p (Full HD)",
            720: "720p (HD)",
            480: "480p",
            360: "360p",
        }
        selected_res = st.selectbox(
            "Qualidade do vídeo:",
            options=available_res,
            format_func=lambda r: res_labels.get(r, f"{r}p"),
        )
        selected_bitrate = "192"
    else:
        selected_res = 1080
        selected_bitrate = st.selectbox(
            "Qualidade do áudio MP3:",
            options=["320", "256", "192", "128"],
            index=0,
            format_func=lambda b: f"{b} kbps",
        )

    if st.button("⬇️ Baixar agora", type="primary", use_container_width=True):
        progress_bar = st.progress(0, text="Iniciando transferência...")
        status_text = st.empty()

        def progress_hook(d: dict):
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("_speed_str", "").strip()
                eta = d.get("_eta_str", "").strip()
                if total and total > 0:
                    pct = min(max(downloaded / total, 0.0), 1.0)
                    progress_bar.progress(
                        pct,
                        text=f"Baixando: {pct * 100:.1f}% • Velocidade: {speed} • Restante: {eta}",
                    )
                else:
                    status_text.caption(f"Transferindo... {speed}")
            elif status == "finished":
                progress_bar.progress(1.0, text="Finalizando e convertendo com FFmpeg...")

        try:
            saved_path = download_media(
                url=st.session_state["video_url"],
                mode=mode,
                resolution=selected_res,
                audio_bitrate=selected_bitrate,
                preferred_client=st.session_state.get("preferred_client", "web_embedded"),
                progress_callback=progress_hook,
            )
            status_text.empty()
            progress_bar.progress(1.0, text="Concluído!")

            size_mb = saved_path.stat().st_size / (1024 * 1024)
            if is_local_windows():
                st.success(f"✅ Arquivo salvo direto na sua pasta Downloads ({size_mb:.1f} MB):\n\n`{saved_path}`")
            else:
                st.success(f"✅ Pronto! Tamanho: {size_mb:.1f} MB")
                with open(saved_path, "rb") as f:
                    st.download_button(
                        label="💾 Salvar arquivo no computador",
                        data=f.read(),
                        file_name=saved_path.name,
                        mime="video/mp4" if mode == "Vídeo (MP4)" else "audio/mpeg",
                        use_container_width=True,
                    )
        except Exception as err:
            st.error(str(err))
