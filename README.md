# Editor de Vídeo Automático

Aplicação web para enviar um vídeo e receber uma versão editada automaticamente, com processamento real via FFmpeg.

## Recursos
- Upload de MP4, MOV, MKV ou WebM (até 1 GB)
- Presets: equilibrado, dinâmico e suave
- Prévia local do vídeo, corte com início e fim em segundos
- Envio de capa JPG/PNG/WebP aplicada aos 2 segundos iniciais
- Controle ajustável de intensidade dos efeitos de imagem (0 a 100%)
- Correção de contraste, saturação, brilho e nitidez
- Redução de ruído visual
- Redução de ruído do áudio, compressão e normalização de volume
- Zoom sutil e movimento de enquadramento no preset dinâmico
- Remoção opcional de pausas silenciosas longas (quando há áudio)
- Música de fundo opcional, enviada pelo usuário
- Progresso real, visualização e download em MP4 H.264/AAC

## Iniciar com Docker

\`\`\`bash
docker compose up --build -d
\`\`\`

Abra http://localhost:8080.

## Instalação manual

Requer Python 3.11+, FFmpeg e FFprobe no PATH.

\`\`\`bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
\`\`\`

## Observações
- O motor usa filtros determinísticos FFmpeg; **não** faz reconstrução generativa de imagens, transcrição ou análise semântica por IA nesta versão.
- O zoom aplicado é sutil; não há detecção automática de rostos.
- Música só é adicionada quando fornecida pelo usuário; use trilhas com direitos adequados.
- Arquivos são mantidos em \`/data\` (volume Docker). Não publique na internet sem autenticação, limite de usuários e política de retenção.
- A duração e a carga de renderização dependem do vídeo e da CPU disponível.
