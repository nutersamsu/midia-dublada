#!/usr/bin/env bash
# Atalhos do dia a dia. Uso: sudo ./gerenciar.sh [comando]   (sem comando = menu)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -f .env ]] || { echo "Ainda não instalado. Rode: sudo ./instalar.sh"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Rode com sudo: sudo ./gerenciar.sh"; exit 1; }
set -a; . ./.env; set +a
export COMPOSE_FILE

status() {
  docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
  echo; echo "Endereços: veja LEIA-ME-INSTALACAO.txt"
}
atualizar() {
  echo "Baixando versões novas..."
  docker compose pull --quiet --ignore-pull-failures
  docker compose up -d --remove-orphans
  docker image prune -f >/dev/null
  echo "Atualizado. As configurações foram mantidas."
}
reconfigurar() {
  docker compose stop qbittorrent >/dev/null 2>&1 || true
  docker compose run --rm --no-deps configurador python -u /app/configurar.py preparar
  docker compose up -d
  docker compose run --rm configurador
}
logs() {
  local s=${1:-}
  if [[ -z $s ]]; then read -rp "Qual app (radarr, sonarr, lidarr, prowlarr, qbittorrent, jellyfin, seerr, manutencao, gluetun)? " s; fi
  docker compose logs --tail 200 -f "$s"
}
backup() {
  local arq="backup-config-$(date +%Y%m%d-%H%M).tar.gz"
  echo "Parando os apps por alguns segundos para copiar os bancos de dados..."
  docker compose stop >/dev/null
  tar --exclude='config/jellyfin-cache' --exclude='*/logs/*' --exclude='*/MediaCover/*' -czf "$arq" .env config
  docker compose up -d >/dev/null
  chmod 600 "$arq"
  echo "Backup das configurações em $(pwd)/$arq (filmes e séries não entram)."
}
ip_vpn() {
  [[ $COMPOSE_FILE == *vpn* ]] || { echo "VPN não está ativada (rode o instalar.sh para ativar)."; return; }
  echo "IP público dos torrents: $(docker compose exec -T gluetun wget -qO- https://ipinfo.io/ip)"
}

case "${1:-}" in
  status) status ;;
  atualizar) atualizar ;;
  reconfigurar) reconfigurar ;;
  logs) logs "${2:-}" ;;
  parar) docker compose stop ;;
  iniciar) docker compose up -d ;;
  reiniciar) docker compose restart ;;
  backup) backup ;;
  vpn) ip_vpn ;;
  "")
    PS3=$'\nEscolha um número: '
    select op in "Ver status" "Atualizar tudo" "Reconfigurar (refaz as ligações entre os apps)" \
                 "Ver logs de um app" "Backup das configurações" "Ver IP da VPN" "Parar tudo" "Iniciar tudo" "Sair"; do
      case $REPLY in
        1) status ;; 2) atualizar ;; 3) reconfigurar ;; 4) logs ;; 5) backup ;; 6) ip_vpn ;;
        7) docker compose stop ;; 8) docker compose up -d ;; *) break ;;
      esac
      break
    done ;;
  *) echo "Comandos: status | atualizar | reconfigurar | logs [app] | backup | vpn | parar | iniciar | reiniciar"; exit 1 ;;
esac
