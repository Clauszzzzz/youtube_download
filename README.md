# YouTube Downloader — Streamlit Cloud

Aplicação web baseada em Streamlit + yt-dlp + FFmpeg.

## Estrutura

```text
.
├── app.py
├── requirements.txt
├── packages.txt
└── README.md
```

## Deploy sem instalar nada no computador

O deploy recomendado é o Streamlit Community Cloud.

1. Crie um repositório no GitHub.
2. Envie estes arquivos para a raiz do repositório.
3. Acesse https://share.streamlit.io/
4. Entre com sua conta GitHub.
5. Clique em "Create app".
6. Selecione o repositório, branch e `app.py`.
7. Clique em Deploy.

O `requirements.txt` instala as dependências Python.
O `packages.txt` faz o Community Cloud instalar o FFmpeg via apt.

O pacote `deno` fornece um binário Deno para o ambiente Python. Ele é usado
pelo yt-dlp para os desafios JavaScript atuais do YouTube.

## Observações

- O arquivo final é mantido em memória pelo `st.download_button`.
- Esta implementação limita o resultado a 500 MB para evitar consumo excessivo
  de memória.
- O servidor precisa ter acesso de saída à internet.
- Alguns vídeos podem não estar disponíveis por região, idade, privacidade,
  autenticação ou limitações impostas pelo próprio YouTube.
- O formato MP4 prioriza streams de vídeo MP4 para evitar recodificação
  desnecessária.
- Use o serviço somente para conteúdo que você tenha autorização para baixar.

## Licenciamento

Consulte as licenças e termos dos projetos Streamlit, yt-dlp, FFmpeg e Deno.
