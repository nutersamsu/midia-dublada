#!/usr/bin/env bash
# Instalador do arr-dublado-ptbr. Uso:   sudo ./instalar.sh
# Pode rodar de novo quando quiser (para mudar respostas ou consertar algo): nada é apagado.
# Modo sem perguntas (avançado): sudo ./instalar.sh --automatico   (usa o .env existente ou os padrões)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

AUTO=0; [[ "${1:-}" == "--automatico" ]] && AUTO=1
if [[ -t 1 ]]; then N=$'\e[1m'; V=$'\e[32m'; A=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; F=$'\e[0m'; else N= V= A= R= C= F=; fi

titulo() { echo; echo "${N}${C}━━ $* ━━${F}"; }
info()   { echo "  $*"; }
ok()     { echo "  ${V}✔${F} $*"; }
alerta() { echo "  ${A}⚠${F} $*"; }
erro()   { echo; echo "  ${R}✖ $*${F}" >&2; exit 1; }

# pergunta "texto" padrão  -> resposta em $RESP
pergunta() {
  local texto=$1 padrao=${2:-}
  if (( AUTO )); then RESP=$padrao; return; fi
  if [[ -n $padrao ]]; then read -rp "  $texto [${padrao}]: " RESP || true; else read -rp "  $texto: " RESP || true; fi
  RESP=${RESP:-$padrao}
}
sim_nao() {  # sim_nao "texto" s|n  -> retorna 0 se sim
  local padrao=$2 dica; [[ $padrao == s ]] && dica="S/n" || dica="s/N"
  if (( AUTO )); then [[ $padrao == s ]]; return; fi
  read -rp "  $1 [$dica]: " RESP || true
  RESP=$(echo "${RESP:-$padrao}" | tr 'A-Z' 'a-z')
  [[ $RESP == s* || $RESP == y* ]]
}
senha_secreta() {
  if (( AUTO )); then RESP=${SENHA:-}; return; fi
  local s1 s2
  while :; do
    read -rsp "  Senha (mínimo 8 caracteres; Enter = gerar uma): " s1 || true; echo
    if [[ -z $s1 ]]; then RESP=$(tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 14); info "Senha gerada: ${N}$RESP${F} (anote!)"; return; fi
    (( ${#s1} >= 8 )) || { alerta "Muito curta."; continue; }
    [[ $s1 != *[\"\'\$\`\\\ ]* ]] || { alerta "Evite aspas, \$, \`, \\ e espaços na senha."; continue; }
    read -rsp "  Repita a senha: " s2 || true; echo
    [[ $s1 == "$s2" ]] && { RESP=$s1; return; } || alerta "As senhas não conferem."
  done
}
porta_livre() {  # porta_livre 7878 -> imprime a primeira porta livre a partir dela
  local p=$1
  while ss -Hltn "sport = :$p" 2>/dev/null | grep -q . || [[ " ${USADAS:-} " == *" $p "* ]]; do p=$((p+1)); done
  USADAS="${USADAS:-} $p"; echo "$p"
}

clear 2>/dev/null || true
cat <<EOF
${N}${C}
   ╔═══════════════════════════════════════════════════════╗
   ║   arr-dublado-ptbr — filmes, séries e música no       ║
   ║   automático, só versões DUBLADAS em português        ║
   ╚═══════════════════════════════════════════════════════╝${F}

  Vai instalar: Jellyfin (assistir), Seerr (pedir), Radarr (filmes),
  Sonarr (séries), Lidarr (música), Prowlarr (fontes), qBittorrent
  (downloads), FlareSolverr e, se você quiser, uma VPN.

  Responda as perguntas; o valor entre [colchetes] é o padrão (só aperte Enter).
EOF

# ------------------------------------------------------------ requisitos
titulo "1/6 Conferindo o computador"
[[ $EUID -eq 0 ]] || erro "Rode com sudo:   sudo ./instalar.sh"
[[ $(uname -s) == Linux ]] || erro "Este instalador é para Linux (Debian, Ubuntu, Raspberry Pi OS, OMV, Proxmox...)."
ok "Linux $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-}") — $(uname -m)"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  alerta "O Docker (com o 'docker compose') não está instalado."
  if sim_nao "Instalar o Docker agora pelo instalador oficial (get.docker.com)?" s; then
    command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
    curl -fsSL https://get.docker.com | sh || erro "A instalação do Docker falhou. Veja https://docs.docker.com/engine/install/"
    systemctl enable --now docker >/dev/null 2>&1 || true
  else
    erro "Instale o Docker e rode de novo: https://docs.docker.com/engine/install/"
  fi
fi
docker info >/dev/null 2>&1 || erro "O Docker está instalado mas não está rodando (tente: systemctl start docker)."
ok "Docker $(docker version -f '{{.Server.Version}}' 2>/dev/null) e $(docker compose version --short 2>/dev/null | sed 's/^/compose /')"
command -v ss >/dev/null || alerta "Comando 'ss' não encontrado: não vou conseguir conferir portas ocupadas."

# respostas anteriores (rodando de novo)
if [[ -f .env ]]; then
  ok "Encontrei uma instalação anterior (.env): as respostas dela viram o padrão."
  set -a; . ./.env; set +a
fi

# ------------------------------------------------------------ pastas e usuário
titulo "2/6 Onde guardar os filmes, séries e música"
info "Escolha um disco com bastante espaço. Discos montados agora:"
df -h -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs --output=target,size,avail 2>/dev/null \
  | awk 'NR==1{printf "    %-45s %8s %8s\n","Pasta","Tamanho","Livre";next} {printf "    %-45s %8s %8s\n",$1,$2,$3}' | head -15
echo
pergunta "Pasta principal (será criada; dentro dela ficam downloads/ e midia/)" "${PASTA_DADOS:-/srv/midia}"
PASTA_DADOS=$(readlink -m "$RESP")
[[ $PASTA_DADOS == / || $PASTA_DADOS == /root* || $PASTA_DADOS == "$PWD"* ]] && erro "Escolha outra pasta (não use /, /root nem a pasta do instalador)."
mkdir -p "$PASTA_DADOS"
ok "$PASTA_DADOS ($(df -h --output=avail "$PASTA_DADOS" | tail -1 | tr -d ' ') livres)"

DONO_PADRAO=${SUDO_USER:-}
[[ -z $DONO_PADRAO || $DONO_PADRAO == root ]] && DONO_PADRAO=$(getent passwd 1000 | cut -d: -f1 || true)
if [[ -z ${PUID:-} ]]; then
  pergunta "Usuário do Linux dono dos arquivos" "${DONO_PADRAO:-root}"
  id "$RESP" >/dev/null 2>&1 || erro "Usuário $RESP não existe."
  PUID=$(id -u "$RESP"); PGID=$(id -g "$RESP")
fi
ok "Arquivos ficam com o dono $(getent passwd "$PUID" | cut -d: -f1 || echo "$PUID") (uid $PUID, gid $PGID)"

TZ_PADRAO=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo America/Sao_Paulo)
TZ=${TZ:-$TZ_PADRAO}

# ------------------------------------------------------------ login
titulo "3/6 Usuário e senha (o mesmo para todos os painéis)"
pergunta "Usuário" "${USUARIO:-admin}"; USUARIO=$RESP
[[ $USUARIO =~ ^[A-Za-z0-9._-]{3,32}$ ]] || erro "Use só letras, números, ponto, - ou _ (3 a 32)."
if [[ -n ${SENHA:-} ]] && (( ! AUTO )) && sim_nao "Manter a senha atual?" s; then :; else senha_secreta; SENHA=$RESP; fi
(( ${#SENHA} >= 8 )) || erro "A senha precisa ter pelo menos 8 caracteres."

# ------------------------------------------------------------ VPN
titulo "4/6 VPN para os downloads"
info "Com VPN, quem vê os torrents enxerga o IP da VPN, não o da sua casa."
info "Se a VPN cair, os downloads PARAM (não vazam). Precisa de uma VPN paga"
info "(Mullvad, ProtonVPN, NordVPN, Surfshark, AirVPN, PIA, Windscribe...)."
VPN_PADRAO=n; [[ ${COMPOSE_FILE:-} == *vpn* ]] && VPN_PADRAO=s
if sim_nao "Usar VPN?" $VPN_PADRAO; then
  USAR_VPN=1
  info "Nome do provedor como o Gluetun conhece: mullvad, protonvpn, nordvpn, surfshark,"
  info "airvpn, private internet access, windscribe, ivpn, expressvpn, cyberghost..."
  info "(lista completa: https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers)"
  pergunta "Provedor" "${VPN_PROVEDOR:-protonvpn}"; VPN_PROVEDOR=$(echo "$RESP" | tr 'A-Z' 'a-z')
  pergunta "Tipo (wireguard é mais rápido; openvpn usa usuário/senha)" "${VPN_TIPO:-wireguard}"; VPN_TIPO=$RESP
  if [[ $VPN_TIPO == wireguard ]]; then
    info "No site da VPN, gere um arquivo WireGuard; dele você copia PrivateKey e Address."
    pergunta "PrivateKey" "${VPN_WIREGUARD_CHAVE:-}"; VPN_WIREGUARD_CHAVE=$RESP
    pergunta "Address (ex.: 10.2.0.2/32)" "${VPN_WIREGUARD_ENDERECO:-}"; VPN_WIREGUARD_ENDERECO=$RESP
    [[ -n $VPN_WIREGUARD_CHAVE ]] || erro "Sem a PrivateKey a VPN não conecta."
  else
    info "Use o usuário/senha de OpenVPN do painel da VPN (não é o login do site)."
    pergunta "Usuário OpenVPN" "${VPN_OPENVPN_USUARIO:-}"; VPN_OPENVPN_USUARIO=$RESP
    pergunta "Senha OpenVPN" "${VPN_OPENVPN_SENHA:-}"; VPN_OPENVPN_SENHA=$RESP
  fi
  pergunta "País do servidor (em inglês, ex.: Brazil, Netherlands; Enter = qualquer)" "${VPN_PAIS:-}"; VPN_PAIS=$RESP
  [[ -e /dev/net/tun ]] || alerta "/dev/net/tun não existe (num LXC do Proxmox, libere o TUN no container)."
else
  USAR_VPN=0
fi

# ------------------------------------------------------------ preferências
titulo "5/6 Preferências"
info "Compartilhar (semear) depois de baixar ajuda a rede de torrents e é exigido em"
info "sites privados. Não compartilhar economiza internet."
SEMEAR_PADRAO=s; [[ ${SEMEAR:-sim} == nao ]] && SEMEAR_PADRAO=n
if sim_nao "Compartilhar um pouco depois de baixar (até 1x o tamanho ou 7 dias)?" $SEMEAR_PADRAO; then
  SEMEAR=sim
else
  SEMEAR=nao
fi

GPU=0
if [[ -d /dev/dri ]]; then
  GPU_PADRAO=s; [[ -n ${COMPOSE_FILE:-} && ${COMPOSE_FILE} != *gpu* ]] && GPU_PADRAO=n
  sim_nao "Achei uma placa de vídeo (/dev/dri). Usar no Jellyfin para converter vídeo?" $GPU_PADRAO && GPU=1
fi
GRUPO_VIDEO=$(getent group video | cut -d: -f3 || echo 44)
GRUPO_RENDER=$(getent group render | cut -d: -f3 || echo "$GRUPO_VIDEO")

IP_LAN=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
IP_LAN=${IP_LAN:-$(hostname -I | awk '{print $1}')}

# portas: mantém as anteriores; se for instalação nova e a porta estiver ocupada, usa a próxima livre
escolher_porta() {  # escolher_porta VAR padrão
  local var=$1 padrao=$2 atual=${!1:-}
  if [[ -n $atual ]]; then USADAS="${USADAS:-} $atual"; return; fi
  printf -v "$var" '%s' "$(porta_livre "$padrao")"
  [[ ${!var} == "$padrao" ]] || alerta "Porta $padrao ocupada: ${var#PORTA_} vai usar ${!var}"
}
escolher_porta PORTA_JELLYFIN 8096; escolher_porta PORTA_SEERR 5055; escolher_porta PORTA_RADARR 7878
escolher_porta PORTA_SONARR 8989;   escolher_porta PORTA_LIDARR 8686; escolher_porta PORTA_PROWLARR 9696
escolher_porta PORTA_QBITTORRENT 8080; escolher_porta PORTA_TORRENT 6881
ok "Portas escolhidas"

COMPOSE_FILE="docker-compose.yml:compose/qbittorrent.yml"
(( USAR_VPN )) && COMPOSE_FILE="docker-compose.yml:compose/qbittorrent-vpn.yml"
(( GPU )) && COMPOSE_FILE+=":compose/jellyfin-gpu.yml"

# ------------------------------------------------------------ .env
umask 077
cat > .env <<EOF
# Criado pelo instalar.sh em $(date '+%d/%m/%Y %H:%M'). Para mudar algo, rode de novo: sudo ./instalar.sh
# (ou edite aqui e rode: sudo ./gerenciar.sh reconfigurar)
COMPOSE_FILE=$COMPOSE_FILE
NOME_PROJETO=${NOME_PROJETO:-arr-dublado}

# Pastas e dono dos arquivos
PASTA_DADOS=$PASTA_DADOS
PUID=$PUID
PGID=$PGID
TZ=$TZ

# Login de todos os painéis (Jellyfin, Seerr, Radarr, Sonarr, Lidarr, Prowlarr, qBittorrent)
USUARIO=$USUARIO
SENHA='$SENHA'

# Compartilhar depois de baixar (sim/nao), até este ratio ou número de dias
SEMEAR=$SEMEAR
SEMEAR_RATIO=${SEMEAR_RATIO:-1}
SEMEAR_DIAS=${SEMEAR_DIAS:-7}

# Lidarr: procurar 1 álbum faltando a cada 10 min e ignorar shows ao vivo
LIDARR_BUSCA_ESPACADA=${LIDARR_BUSCA_ESPACADA:-sim}
LIDARR_SEM_AO_VIVO=${LIDARR_SEM_AO_VIVO:-sim}

# Endereço onde os painéis respondem (0.0.0.0 = toda a rede local)
IP_PAINEIS=${IP_PAINEIS:-0.0.0.0}
IP_TORRENT=${IP_TORRENT:-0.0.0.0}
IP_LAN=$IP_LAN
PORTA_JELLYFIN=$PORTA_JELLYFIN
PORTA_SEERR=$PORTA_SEERR
PORTA_RADARR=$PORTA_RADARR
PORTA_SONARR=$PORTA_SONARR
PORTA_LIDARR=$PORTA_LIDARR
PORTA_PROWLARR=$PORTA_PROWLARR
PORTA_QBITTORRENT=$PORTA_QBITTORRENT
PORTA_TORRENT=$PORTA_TORRENT

# Placa de vídeo (grupos do sistema)
GRUPO_VIDEO=$GRUPO_VIDEO
GRUPO_RENDER=$GRUPO_RENDER

# VPN (Gluetun) — só vale com COMPOSE_FILE contendo qbittorrent-vpn.yml
VPN_PROVEDOR='${VPN_PROVEDOR:-}'
VPN_TIPO=${VPN_TIPO:-wireguard}
VPN_WIREGUARD_CHAVE='${VPN_WIREGUARD_CHAVE:-}'
VPN_WIREGUARD_ENDERECO='${VPN_WIREGUARD_ENDERECO:-}'
VPN_OPENVPN_USUARIO='${VPN_OPENVPN_USUARIO:-}'
VPN_OPENVPN_SENHA='${VPN_OPENVPN_SENHA:-}'
VPN_PAIS='${VPN_PAIS:-}'
EOF
umask 022
chmod 600 .env
ok "Respostas salvas em .env (só o root lê; contém a senha)"

# ------------------------------------------------------------ subir
titulo "6/6 Baixando e configurando (pode demorar na primeira vez)"
export COMPOSE_FILE
info "Baixando os programas (cerca de 3 GB na primeira vez; pode levar vários minutos)..."
docker compose pull --quiet --ignore-pull-failures 2>&1 | grep -v '^\s*$' || true
docker compose --profile ferramentas pull --quiet configurador >/dev/null 2>&1 || true
ok "Imagens baixadas"

# o qBittorrent precisa estar parado para receber a senha
docker compose stop qbittorrent >/dev/null 2>&1 || true
docker compose run --rm --no-deps configurador python -u /app/configurar.py preparar
docker compose up -d --remove-orphans 2>&1 | grep -vE '^\s*$' | sed 's/^/  /'
if (( USAR_VPN )); then
  info "Esperando a VPN conectar..."
  for _ in $(seq 1 30); do
    [[ $(docker compose ps --format '{{.Health}}' gluetun 2>/dev/null) == healthy ]] && break; sleep 4
  done
  [[ $(docker compose ps --format '{{.Health}}' gluetun 2>/dev/null) == healthy ]] \
    || erro "A VPN não conectou. Veja o motivo:  docker compose logs gluetun   (confira chave/usuário e rode de novo)"
  ok "VPN conectada — IP público dos torrents: $(docker compose exec -T gluetun wget -qO- https://ipinfo.io/ip 2>/dev/null || echo '?')"
fi
docker compose run --rm configurador || erro "A configuração automática parou. Leia a mensagem acima e rode o instalador de novo."

# ------------------------------------------------------------ resumo
U="http://${IP_LAN}"
RESUMO=$(cat <<EOF

${N}${V}══════════════════  TUDO PRONTO!  ══════════════════${F}

  Usuário: ${N}$USUARIO${F}    Senha: ${N}a que você escolheu${F} (está no arquivo .env)

  ${N}Para usar no dia a dia${F}
    Assistir (Jellyfin) ......... $U:$PORTA_JELLYFIN
    Pedir filmes e séries (Seerr)  $U:$PORTA_SEERR

  ${N}Painéis técnicos${F}
    Radarr (filmes) ............. $U:$PORTA_RADARR
    Sonarr (séries) ............. $U:$PORTA_SONARR
    Lidarr (música) ............. $U:$PORTA_LIDARR
    Prowlarr (fontes) ........... $U:$PORTA_PROWLARR
    qBittorrent (downloads) ..... $U:$PORTA_QBITTORRENT

  ${N}${A}Falta só 1 passo:${F} abra o Prowlarr > Indexadores > "+ Adicionar indexador"
  e escolha as fontes de busca que você quer usar. Elas vão sozinhas para
  o Radarr, Sonarr e Lidarr. Depois é só pedir um filme no Seerr.

  Arquivos em: $PASTA_DADOS/midia (filmes, series, musica)
  Comandos úteis:  sudo ./gerenciar.sh   (status, atualizar, logs, parar...)
EOF
)
echo "$RESUMO"
echo "$RESUMO" | sed 's/\x1b\[[0-9;]*m//g' > LEIA-ME-INSTALACAO.txt
chmod 600 LEIA-ME-INSTALACAO.txt
