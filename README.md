# Editor de Vídeo Automático — Estúdio

Editor de vídeo web, com tratamento por trecho, prévia e modo Diretor Automático. Backend FastAPI/Python e FFmpeg.

## Novidades implementadas no código

- **Diretor Automático**: detecta mudanças visuais e silêncios com FFmpeg (sem IA semântica), gera até 30 cenas e sugere estilos conforme perfil **Redes sociais**, **Cinema**, **Igreja**, **Aula** ou **Natural**.
- **Linha do tempo de cenas**: início/fim, reordenação por arrastar, botões ↑ e ↓, zoom, cor, velocidade 0,5–2x e transição fade por cena.
- **Tratamento visual**: estilos natural, cinema, vibrante, quente, frio e P&B; intensidade ajustável, zoom e diferentes proporções de exportação.
- **Áudio**: redução de ruído baseada em filtros, compressor, equalização básica por passa-altas/baixas, normalização, volumes separados e *ducking* para baixar a música enquanto a pessoa fala.
- **Capas**: imagem enviada pelo usuário ou quadro capturado do vídeo em reprodução.
- **Prévia**: comparar tratamento visual aproximado e imagem original, antes de renderizar.
- **Projetos**: salvar/carregar os parâmetros no navegador (localStorage). Arquivos de vídeo não são salvos no navegador; precisam ser selecionados novamente.
- **Legendas SRT**: transcrição **opcional** usando `faster-whisper` local, com arquivo SRT em grupos de até cinco palavras. Também é possível anexar um SRT com tempos relativos ao vídeo final para incorporá-lo como **faixa de legendas selecionável no MP4**. Não há queima de texto sobre a imagem nesta versão.
- Upload com barra de progresso separado da renderização e download de MP4.

## Hospedagem na iContainer — Python

Instale `requirements.txt`, use `app.py` como comando de inicialização. Ele inicia Uvicorn na porta configurada por `PORT`, padrão 8080. FFmpeg é disponibilizado por `imageio-ffmpeg` quando não existe no PATH.

Para habilitar transcrição local, instale também:

```bash
pip install -r requirements-ai.txt
```

Os modelos Whisper são baixados no primeiro uso. Requer CPU/RAM/espaço e acesso à rede para obter o modelo; para vídeos longos use um worker com GPU ou recursos dedicados. Por padrão, a edição funciona sem instalar esse pacote.

Com Docker:

```bash
docker compose up --build -d
```

A análise automática tem limite de **500 MB por envio**, a transcrição de **400 MB** e o upload normal de **4 GB**, sujeito a restrições do proxy, disco e memória da hospedagem. A análise examina até os primeiros 30 minutos; confirme os cortes propostos antes de renderizar.

## Variáveis recomendadas para VPS

- `EDITOR_PASSWORD`: ao definir, ativa autenticação HTTP Basic para todas as rotas.
- `EDITOR_USERNAME`: nome de usuário (padrão `admin`).
- `MAX_PARALLEL_RENDERS`: quantidade máxima de renderizações simultâneas (padrão 1; máximo 4).
- `EDITOR_KEEP_DAYS`: dias para conservar arquivos antes da limpeza acionada por novos envios (padrão 7).
- `PORT`: porta HTTP (padrão 8080).
- `EDITOR_DATA_DIR`: diretório de armazenamento persistente.

A autenticação básica exige **HTTPS**, já que as credenciais não são criptografadas pelo protocolo Basic. A limpeza acontece quando houver um novo upload de edição, não é um agendador contínuo.

## Segurança e limitações

- **Não é equivalente ao CapCut.** Não implementa editor multicamadas completo, legendas já gravadas no MP4, estabilização neural, enquadramento por rosto, remoção de fundo, super-resolução ou detecção semântica de "melhores momentos". São melhorias futuras e podem exigir modelos e GPU.
- A prévia no navegador é apenas aproximada; o render final depende do FFmpeg e da resolução do vídeo original.
- O modo automático é baseado em detecção de cenas e pausas, não interpreta o roteiro ou o conteúdo visual como um humano.
- O estado das tarefas permanece em memória e a renderização em thread é interrompida se o aplicativo reiniciar. Para ambiente multiusuário, usar fila durável e autenticação.
- Não publique na internet sem autenticação, limite de armazenamento, limpeza de arquivos antigos e quotas. Não há limpezas automatizadas nem isolamento entre usuários nesta versão.
- Música, vídeo e imagens precisam de direitos adequados.
- Caso a exportação falhe, verifique os logs e o suporte de filtros do binário FFmpeg utilizado no servidor.

## Validação

Existe workflow `.github/workflows/render-tests.yml` para testar sintaxe e renderização sintética. A presença do arquivo não significa que os testes já tenham passado: verifique a aba **Actions** no GitHub e faça um teste de vídeo curto na iContainer após sincronizar.
