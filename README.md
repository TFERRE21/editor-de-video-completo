# Editor de Vídeo Automático — edição por cenas

Aplicação web em Python/FastAPI e FFmpeg. Envie seu vídeo e crie uma edição com ajustes diferentes por trecho, controles de áudio, efeitos de imagem, cortes, capa e exportação MP4.

## Ferramentas disponíveis
- Upload de vídeo de até **4 GB na aplicação** (pode haver limite inferior do proxy da hospedagem).
- Barra de upload e barra de renderização, independentes.
- Prévia local e seleção de intervalo inicial/final.
- **Linha do tempo por trechos:** até 30 cenas na sequência escolhida; cada trecho recebe início/fim próprios, look, zoom e velocidade.
- Looks: **natural, cinema, vibrante, quente, frio e preto e branco**; intensidade de cor e nitidez ajustável.
- Zoom leve ou próximo por cena.
- Velocidade de reprodução de **0,5x a 2x** por trecho.
- Áudio original, limpeza equilibrada ou modo voz destacada; redução de ruído, compressor e normalização.
- Volume individual de voz e música de fundo opcional.
- Imagem de capa nos dois primeiros segundos.
- Formato HD horizontal, vertical e quadrado.
- Remoção opcional de pausas longas no modo sem cenas manuais.
- Download MP4 H.264/AAC.

## Executar via iContainer (Python)

Aponte a aplicação para o arquivo **app.py** e instale `requirements.txt`. O arquivo inclui o inicializador Uvicorn e o executável FFmpeg é instalado via `imageio-ffmpeg`, dispensando `ffprobe` do sistema.

Em caso de uso local ou VPS:

```bash
docker compose up -d --build
```

Abrir porta 8080 (ou a variável de ambiente `PORT`).

## Limitações importantes
- **Não é o CapCut:** não há editor drag-and-drop, multicamadas, animação de textos, detecção de rostos, legendas automáticas nem reconstrução generativa de qualidade. Os controles são FFmpeg e a prévia dos filtros do navegador é aproximada.
- A intensidade é baseada em ajustes fixos de nitidez/cor, que podem não beneficiar todos os materiais. A limpeza de ruído pode alterar a voz dependendo da gravação.
- A hospedagem precisa de memória, CPU, disco e limites de proxy compatíveis com o tamanho e a duração do arquivo. Projetos maiores exigem mais processamento.
- O progresso é informado pelo FFmpeg e não é uma previsão de tempo restante.
- O estado das tarefas fica na memória do processo; um restart durante a renderização interrompe o trabalho.
- Não exponha a aplicação pública sem autenticação, controle de uso, limite de armazenamento e política de exclusão dos vídeos.
- Use apenas músicas e imagens com os direitos necessários.
