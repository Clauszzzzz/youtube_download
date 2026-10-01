
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import yt_dlp


st.set_page_config(
    page_title="YouTube Downloader",
    page_icon="🎬",
    layout="wide",
)

MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
BGUTIL_VERSION = "2.0.0"
BGUTIL_DIR = Path.home() / ".cache" / "bgutil-ytdlp-pot-provider"
BGUTIL_SERVER = BGUTIL_DIR / "server"


def formatar_duracao(segundos: Optional[int]) -> str:
    if not segundos:
        return "N/A"
    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas:02d}:{minutos:02d}:{segundos:02d}" if horas else f"{minutos:02d}:{segundos:02d}"


def formatar_visualizacoes(valor: Optional[int]) -> str:
    if valor is None:
        return "N/A"
    if valor >= 1_000_000_000:
        return f"{valor / 1_000_000_000:.1f} bi"
    if valor >= 1_000_000:
        return f"{valor / 1_000_000:.1f} mi"
    if valor >= 1_000:
        return f"{valor / 1_000:.1f} mil"
    return str(valor)


def formatar_bytes(valor: Optional[float]) -> str:
    if valor is None:
        return "N/A"
    valor = float(valor)
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024:
            return f"{valor:.1f} {unidade}"
        valor /= 1024
    return f"{valor:.1f} PB"


def formatar_velocidade(valor: Optional[float]) -> str:
    return "N/A" if valor is None else f"{formatar_bytes(valor)}/s"


def formatar_eta(valor: Optional[int]) -> str:
    if valor is None:
        return "N/A"
    minutos, segundos = divmod(int(valor), 60)
    return f"{minutos}m {segundos}s" if minutos else f"{segundos}s"


def sanitizar_nome_arquivo(nome: str, limite: int = 180) -> str:
    nome = nome or "download"
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", nome)
    nome = re.sub(r"\s+", " ", nome).strip().rstrip(". ")
    return (nome or "download")[:limite]


def normalizar_url(url: str) -> str:
    url = url.strip()
    if "youtube.com/playlist" in url:
        raise ValueError("Playlists não são suportadas. Cole o link de um vídeo individual.")
    if "youtube.com/watch" in url and "&list=" in url:
        url = url.split("&list=", 1)[0]
    if "youtu.be/" in url and "?" in url:
        url = url.split("?", 1)[0]
    return url


def iniciar_bgutil_provider() -> None:
    """
    Inicializa o provider de PO Tokens localmente no servidor.
    O provider é usado pelo plugin bgutil do yt-dlp.
    """
    marker = BGUTIL_SERVER / ".ready"
    pid_file = BGUTIL_DIR / "provider.pid"

    if marker.exists():
        # Verifica se há um servidor respondendo.
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=1):
                return
        except Exception:
            pass

    BGUTIL_DIR.mkdir(parents=True, exist_ok=True)

    # Baixa o código do provider apenas no servidor, se ainda não existir.
    archive = BGUTIL_DIR / f"bgutil-{BGUTIL_VERSION}.tar.gz"
    if not BGUTIL_SERVER.exists():
        url = (
            f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/"
            f"archive/refs/tags/{BGUTIL_VERSION}.tar.gz"
        )
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(BGUTIL_DIR, filter="data")
        extracted = BGUTIL_DIR / f"bgutil-ytdlp-pot-provider-{BGUTIL_VERSION}"
        extracted.rename(BGUTIL_SERVER)

    # Deno é instalado pelo requirements.txt.
    deno = shutil.which("deno")
    if not deno:
        raise RuntimeError(
            "Deno não foi encontrado no servidor. "
            "Verifique requirements.txt."
        )

    # Instala as dependências do servidor conforme a documentação do provider.
    node_modules = BGUTIL_SERVER / "node_modules"
    if not node_modules.exists():
        subprocess.run(
            [
                deno,
                "install",
                "--allow-scripts=npm:canvas",
                "--frozen",
            ],
            cwd=BGUTIL_SERVER,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )

    # Evita iniciar múltiplas instâncias.
    try:
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=1):
            marker.touch()
            return
    except Exception:
        pass

    cmd = [
        deno,
        "run",
        "--allow-env",
        "--allow-net",
        "--allow-ffi=.",
        "--allow-read=.",
        "../src/main.ts",
    ]

    log_file = BGUTIL_DIR / "provider.log"
    log_handle = open(log_file, "a", encoding="utf-8")

    process = subprocess.Popen(
        cmd,
        cwd=node_modules,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    pid_file.write_text(str(process.pid), encoding="utf-8")

    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=1):
                marker.touch()
                return
        except Exception:
            time.sleep(1)

    log_handle.close()
    log = log_file.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise RuntimeError(
        "Não foi possível iniciar o provider de PO Tokens.\n\n"
        f"Log do provider:\n{log}"
    )


@st.cache_resource
def preparar_ambiente() -> bool:
    try:
        iniciar_bgutil_provider()
        return True
    except Exception as exc:
        st.session_state["bgutil_error"] = str(exc)
        return False


def extrair_info_video(url: str) -> Dict[str, Any]:
    if not url or not url.strip():
        raise ValueError("Informe uma URL do YouTube.")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "js_runtimes": {"deno": {}},
        "remote_components": {"ejs": ["github"]},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalizar_url(url), download=False)

        if not info:
            raise RuntimeError("O YouTube não retornou informações para essa URL.")
        return info

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        low = msg.lower()
        if "private video" in low:
            raise RuntimeError("O vídeo é privado e não pode ser acessado.") from exc
        if "sign in" in low or "confirm your age" in low or "age" in low:
            raise RuntimeError("O vídeo possui restrição de idade ou exige autenticação.") from exc
        if "not available" in low or "unavailable" in low:
            raise RuntimeError("O vídeo está indisponível ou possui restrição regional.") from exc
        raise RuntimeError(f"Não foi possível acessar o vídeo: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"Erro ao analisar o vídeo: {exc}") from exc


def obter_formatos_disponiveis(info_dict: Dict[str, Any]) -> Dict[str, Any]:
    resolucoes = set()
    videos = []
    audios = []

    for fmt in info_dict.get("formats", []):
        ext = fmt.get("ext")
        height = fmt.get("height")
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")

        if ext == "mp4" and vcodec not in (None, "none") and height:
            resolucoes.add(int(height))
            videos.append(fmt)

        if acodec not in (None, "none") and vcodec in (None, "none"):
            audios.append(fmt)

    return {
        "resolucoes": sorted(resolucoes, reverse=True),
        "video": videos,
        "audio": audios,
    }


class DownloadProgress:
    def __init__(self) -> None:
        self.progress_bar = None
        self.status = None

    def iniciar(self) -> None:
        self.progress_bar = st.progress(0)
        self.status = st.empty()

    def hook(self, data: Dict[str, Any]) -> None:
        if self.progress_bar is None or self.status is None:
            return

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)

            if total:
                percentual = min(max(downloaded / total, 0), 1)
                self.progress_bar.progress(int(percentual * 100))
                percentual_texto = f"{percentual * 100:.1f}%"
            else:
                percentual_texto = "calculando"

            self.status.info(
                f"**Baixando:** {percentual_texto}  \n"
                f"**Velocidade:** {formatar_velocidade(data.get('speed'))} • "
                f"**Tempo restante:** {formatar_eta(data.get('eta'))}"
            )

        elif status == "finished":
            self.progress_bar.progress(100)
            self.status.info("Download concluído. Finalizando com FFmpeg...")


def _arquivo_final(pasta: str, extensao: str) -> Path:
    candidatos = [
        p for p in Path(pasta).iterdir()
        if p.is_file() and p.suffix.lower() == extensao.lower()
    ]
    if not candidatos:
        raise FileNotFoundError(
            f"O processamento terminou, mas nenhum arquivo {extensao} foi encontrado."
        )
    return max(candidatos, key=lambda p: p.stat().st_mtime)


def baixar_e_converter(
    url: str,
    formato_escolhido: str,
    qualidade: int,
    pasta_destino: str,
    progress: Optional[DownloadProgress] = None,
) -> Path:
    if progress:
        progress.iniciar()

    common = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 4,
        "fragment_retries": 4,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress.hook] if progress else [],
        "outtmpl": str(Path(pasta_destino) / "%(title).180s.%(ext)s"),
        "js_runtimes": {"deno": {}},
        "remote_components": {"ejs": ["github"]},

        # PO Token provider.
        # O provider HTTP local é detectado pelo plugin.
        "extractor_args": {
            "youtubepot-bgutilhttp": {
                "base_url": "http://127.0.0.1:4416"
            },
            "youtube": {
                "player_client": ["mweb", "web_safari", "web_embedded"]
            },
        },
    }

    if formato_escolhido == "video":
        common.update({
            "format": (
                f"bestvideo[height<={qualidade}][ext=mp4]+"
                f"bestaudio[ext=m4a]/"
                f"bestvideo[height<={qualidade}]+"
                f"bestaudio/"
                f"best[height<={qualidade}]"
            ),
            "merge_output_format": "mp4",
        })
        extensao_final = ".mp4"

    elif formato_escolhido == "audio":
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(qualidade),
            }],
        })
        extensao_final = ".mp3"

    else:
        raise ValueError("Tipo de mídia inválido.")

    try:
        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.download([normalizar_url(url)])
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        low = msg.lower()

        if "403" in low or "forbidden" in low:
            raise RuntimeError(
                "O YouTube recusou o stream (HTTP 403). "
                "O servidor tentou usar o provider de PO Token, "
                "mas este vídeo/IP pode exigir uma estratégia diferente."
            ) from exc

        if "ffmpeg" in low or "ffprobe" in low:
            raise RuntimeError(
                "FFmpeg/FFprobe não está disponível no servidor."
            ) from exc

        raise RuntimeError(f"Falha no download: {msg}") from exc

    except Exception as exc:
        raise RuntimeError(f"Erro durante o processamento: {exc}") from exc

    return _arquivo_final(pasta_destino, extensao_final)


def mostrar_metadados(info: Dict[str, Any]) -> None:
    esquerda, direita = st.columns([1, 2])

    with esquerda:
        if info.get("thumbnail"):
            st.image(info["thumbnail"], use_container_width=True)

    with direita:
        titulo = info.get("title") or info.get("fulltitle") or "Título indisponível"
        st.subheader(titulo)
        st.write(f"**Canal:** {info.get('channel') or info.get('uploader') or 'N/A'}")
        st.write(f"**Duração:** {formatar_duracao(info.get('duration'))}")
        st.write(f"**Visualizações:** {formatar_visualizacoes(info.get('view_count'))}")


def main() -> None:
    st.title("🎬 YouTube Downloader")
    st.caption("MP4 com áudio ou extração de áudio MP3 — processamento realizado no servidor.")
    st.info("Use somente conteúdo que você tenha autorização para baixar ou que seja permitido pelos termos e direitos aplicáveis.")

    # Inicializa o provider antes de qualquer download.
    ambiente_ok = preparar_ambiente()
    if not ambiente_ok:
        with st.expander("Diagnóstico do servidor"):
            st.warning(st.session_state.get("bgutil_error", "Falha desconhecida"))
        st.error("O componente de acesso ao YouTube não foi inicializado. Reinicie o app e tente novamente.")

    url = st.text_input("URL do YouTube", placeholder="https://www.youtube.com/watch?v=...")

    if st.button("🔎 Analisar Vídeo", type="primary", use_container_width=True):
        if not url.strip():
            st.warning("Cole uma URL antes de continuar.")
        else:
            with st.spinner("Analisando o vídeo..."):
                try:
                    info = extrair_info_video(url)
                    st.session_state["video_info"] = info
                    st.session_state["video_url"] = normalizar_url(url)
                except Exception as exc:
                    st.error(str(exc))

    info = st.session_state.get("video_info")
    if not info:
        return

    st.divider()
    mostrar_metadados(info)

    formatos = obter_formatos_disponiveis(info)
    st.divider()
    st.subheader("⚙️ Download")

    tipo = st.radio(
        "Tipo de mídia",
        ["Vídeo MP4 (Com Áudio)", "Apenas Áudio MP3"],
        horizontal=True,
    )

    if tipo == "Vídeo MP4 (Com Áudio)":
        resolucoes = formatos["resolucoes"]
        if not resolucoes:
            st.warning("Nenhum stream de vídeo MP4 foi encontrado.")
            return

        # Normaliza a apresentação das resoluções.
        opcoes = {}
        for altura in resolucoes:
            if altura >= 2160:
                label = f"{altura}p / 4K"
            elif altura >= 1440:
                label = f"{altura}p / 2K"
            elif altura >= 1080:
                label = "1080p"
            elif altura >= 720:
                label = "720p"
            elif altura >= 480:
                label = "480p"
            elif altura >= 360:
                label = "360p"
            elif altura >= 240:
                label = "240p"
            else:
                label = f"{altura}p"
            opcoes.setdefault(label, altura)

        escolha = st.selectbox("Qualidade", list(opcoes))
        qualidade = opcoes[escolha]
        formato = "video"
        st.caption("O servidor combina o melhor vídeo disponível até a resolução selecionada com o melhor áudio e usa FFmpeg para o merge.")

    else:
        opcoes = {
            "320 kbps — Alta qualidade": 320,
            "192 kbps — Média qualidade": 192,
            "128 kbps — Padrão": 128,
        }
        escolha = st.selectbox("Qualidade do áudio", list(opcoes))
        qualidade = opcoes[escolha]
        formato = "audio"
        st.caption(f"O áudio será convertido para MP3 em {qualidade} kbps com FFmpeg.")

    st.divider()

    if st.button("⬇️ Baixar arquivo", type="primary", use_container_width=True):
        if not ambiente_ok:
            st.error("O provider do servidor não está disponível.")
            return

        progress = DownloadProgress()

        try:
            with tempfile.TemporaryDirectory(prefix="yt_") as temp_dir:
                arquivo = baixar_e_converter(
                    st.session_state["video_url"],
                    formato,
                    qualidade,
                    temp_dir,
                    progress,
                )

                tamanho = arquivo.stat().st_size
                if tamanho > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("O arquivo final ultrapassou o limite de 500 MB.")

                dados = arquivo.read_bytes()
                titulo = sanitizar_nome_arquivo(info.get("title") or "download")

                if formato == "video":
                    nome = f"{titulo}.mp4"
                    mime = "video/mp4"
                else:
                    nome = f"{titulo}.mp3"
                    mime = "audio/mpeg"

                st.success(f"Arquivo pronto — {formatar_bytes(tamanho)}")
                st.download_button(
                    "💾 Salvar no computador",
                    data=dados,
                    file_name=nome,
                    mime=mime,
                    use_container_width=True,
                )
        except Exception as exc:
            st.error(str(exc))


if __name__ == "__main__":
    main()
