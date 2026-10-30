# CRM fictício

Base **inventada** para o desafio: nenhum lojista, valor, serial ou operadora aqui é real, e não há integração com
sistemas Getnet. `merchants.json` mapeia `user_id` → perfil, vendas de ontem, recebíveis, transações e terminal.

- `cliente1988` é o lojista dos 10 cenários do enunciado (terminal offline por Wi-Fi fraco, recusas 51/05/55).
- `cliente2020` existe só para testar acesso cruzado: seus valores (99999, 777777, TX-2001) nunca podem aparecer
  numa resposta para `cliente1988`.
- Datas são relativas (`days_ago`, `last_seen_minutes_ago`) e viram datas reais no momento da consulta.
