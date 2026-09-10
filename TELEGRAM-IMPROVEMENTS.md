# Melhorias de interação e entrega

As legendas mantêm apenas qualidade, profundidade, amostragem e taxa de bits, nessa ordem.

## Funcionalidades

* 1, 2, 3, 4: maior e menor arquivo válido, volume confirmado e duração musical conhecida.
* 7, 8, 9: contagem por qualidade e amostragem; faixa com maior profundidade/amostragem.
* 11, 12, 13: tentativas, tempo acumulado, falhas recuperadas, pendências categorizadas e estatísticas persistentes por arquivo. Tamanho, duração e ID da mensagem são registrados junto à confirmação, antes da exclusão. Metadados de pedidos antigos que não foram registrados não são inventados.
* 14: busca por música, álbum ou artista com dez resultados por página, artista/álbum, botão para a capa e seleção no Telegram.
* 15, 16: escolha de faixas e álbuns por botões ou números como `1,3,7-12`, com confirmação. O downloader recebe URLs concretas e não aguarda entradas no terminal.
* 17: faixa única preservada também no caminho de análise de qualidade dos dois downloaders.
* 23: preferências individuais persistentes, recuperadas pelo botão **Usar preferência salva**. Mudanças são salvas ao confirmar um pedido.
* 25: prévia de quantidade, duração conhecida e faixa estimada de volume; indisponibilidade e estimativas parciais são explícitas.
* 36, 37: classificação de falhas e espaçamento adaptativo após limites do Telegram, respeitando `retry_after`, inclusive entre arquivos.
* 38: `/saude` consulta serviços e espaço livre; detalhes administrativos exigem `TELEGRAM_ADMIN_USERS` com IDs separados por vírgula. Nenhum token é mostrado.
* 40: casos de aceitação equivalentes em Go/Python para seleção, classificação, persistência, preferências, cancelamento e consultas reais opcionais.

Os registros ficam em `.jobs` e as preferências em `.preferences`, dentro do volume de downloads. Esse volume deve ser preservado nas atualizações. Em conversas privadas, o resumo mostra o ID da mensagem; links diretos `t.me/c` só são gerados para supergrupos/canais compatíveis.

## Validação

Go: `go test ./...`, `go test -race ./cmd/telegram-bot`, `go vet ./...`.

Python: `python -m pytest -q`.

Os fixtures `cmd/telegram-bot/testdata/acceptance.json` e `tests/acceptance.json` são iguais nos dois repositórios. Os testes locais simulam Telegram/catálogo e não enviam músicas.

No ambiente autenticado do servidor, `AMDL_LIVE_ACCEPTANCE=1` habilita os testes de catálogo real: uma música, paginação da playlist, álbuns de artista e análise de qualidade de exatamente uma faixa. Go: selecionar `TestLiveCatalogAndSingleTrack`; Python: `tests/test_acceptance.py::test_live_catalog_and_single_track`. Esses testes consultam metadados, sem enviar mídia. As opções de codec/resolução têm testes de roteamento; isso não comprova a disponibilidade de cada formato para todo item do catálogo.

O script `update_bots.sh` continua usando o volume persistente e pode selecionar a implementação Go ou Python. Não execute as duas com o mesmo token simultaneamente.
