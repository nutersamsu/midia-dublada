#!/usr/bin/env python3
"""Manutenção automática (container "manutencao"). A cada 5 minutos:

1. Torrent com "Forçar início" ignora o limite de compartilhamento: tira o forçado dos
   torrents radarr/sonarr/lidarr que já terminaram, para eles saírem do qBittorrent.
2. Radarr travado em "matched to movie by ID": importa o arquivo.
3. Download feito À MÃO com categoria radarr/sonarr de algo que não está cadastrado:
   - Filme: cadastra no Radarr se o TMDB der UM resultado com mesmo título e ano.
   - Série: o Sonarr guarda o nome em inglês e não reconhece o nome em português do torrent.
     Se a busca aponta para uma série JÁ cadastrada, importa os episódios nela; série nova só
     é cadastrada se UM resultado tiver exatamente o mesmo nome.
   Caso ambíguo: não mexe, só registra no log.
4. Versão que promete resolução maior do que o arquivo tem (nome 1080p, arquivo 720p):
   bloqueia essa versão para não baixar de novo em loop. O arquivo fica na biblioteca.
5. Lidarr (a cada 10 min): procura UM álbum faltando por vez, para não cair no limite das fontes,
   e desmarca álbuns com cara de show ao vivo.

Ver o que ela fez:  docker compose logs manutencao
Simular sem mudar nada:  docker compose run --rm manutencao python /app/manutencao.py --simular --uma-vez
"""
import datetime as dt, http.cookiejar, json, os, re, sys, time, unicodedata
import urllib.parse as q, urllib.request as u

SIMULAR = "--simular" in sys.argv
UMA_VEZ = "--uma-vez" in sys.argv
ENV = os.environ
SIM = ("sim", "s", "yes", "y", "true", "1")
INTERVALO = 300
INTERVALO_LIDARR = 600
BLOQUEIO = "matched to movie by ID"
VIDEO = (".mkv", ".mp4", ".avi", ".m4v")
ESPERA_CADASTRO = 180    # s depois de concluir, para o app ter a chance de importar sozinho
JANELA_LOOP_H = 48
FILMES, SERIES = "/data/midia/filmes", "/data/midia/series"
QBIT = "http://qbittorrent:" + ENV.get("PORTA_QBITTORRENT", "8080")
APPS = {"radarr": ("http://radarr:7878/api/v3/", "radarr"),
        "sonarr": ("http://sonarr:8989/api/v3/", "sonarr"),
        "lidarr": ("http://lidarr:8686/api/v1/", "lidarr")}
SHOW = re.compile(r"\blive\b|ao vivo|unplugged|\b(19|20)\d\d[-/.‐]\d\d[-/.‐]\d\d\b|\b\d\d?\.\d\d?\.\d\d\b"
                  r"|festival|\bconcert|\bsessions?\b|\btour\b|\bbbc\b|broadcast|bootleg"
                  r"|\bin (concert|tokyo|london|paris|texas|berlin|rio)\b|rock in rio", re.I)


def log(*a):
    print(dt.datetime.now().strftime("%d/%m %H:%M"), *a, flush=True)


# ---------------------------------------------------------------- acesso
_qbit = None


def qbit(caminho, dados=None):
    global _qbit
    if _qbit is None:
        _qbit = u.build_opener(u.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        r = _qbit.open(u.Request(QBIT + "/api/v2/auth/login", headers={"Referer": QBIT},
                                 data=q.urlencode({"username": ENV.get("USUARIO", "admin"), "password": ENV.get("SENHA", "")}).encode()),
                       timeout=15).read()
        if r.strip() == b"Fails.":  # qBittorrent 5 responde 204 vazio quando dá certo
            _qbit = None
            raise RuntimeError("qBittorrent recusou usuário/senha do .env")
    req = u.Request(QBIT + "/api/v2/" + caminho, headers={"Referer": QBIT},
                    data=dados.encode() if dados else None)
    try:
        return _qbit.open(req, timeout=30).read().decode()
    except u.HTTPError as e:
        if e.code == 403:  # sessão expirou
            _qbit = None
        raise


def arr(app, caminho, corpo=None, metodo=None):
    url, pasta = APPS[app]
    chave = re.search(r"<ApiKey>(.*?)<", open(f"/config-apps/{pasta}/config.xml").read()).group(1)
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = u.Request(url + caminho, data=dados, method=metodo,
                  headers={"X-Api-Key": chave, "Content-Type": "application/json"})
    resp = u.urlopen(r, timeout=120).read()
    return json.loads(resp) if resp.strip() else {}


def radarr(c, corpo=None):
    return arr("radarr", c, corpo)


def sonarr(c, corpo=None):
    return arr("sonarr", c, corpo)


def perfil_id(app):
    """O perfil criado pelo instalador começa com 🇧🇷 (ex.: "🇧🇷 Dublado (até 1080p)")."""
    return next((p["id"] for p in arr(app, "qualityprofile") if p["name"].startswith("🇧🇷")), 1)


def normal(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", txt.replace("&", "and"))


def nomes(item):
    return {normal(item.get("title")), normal(item.get("originalTitle"))} | \
           {normal(a.get("title")) for a in item.get("alternateTitles", [])}


def concluidos(categoria):
    agora = time.time()
    return [t for t in json.loads(qbit("torrents/info?" + q.urlencode({"category": categoria})))
            if t["amount_left"] == 0 and t.get("completion_on", 0) > 0 and agora - t["completion_on"] >= ESPERA_CADASTRO]


# ---------------------------------------------------------------- tarefas
def tirar_forcado():
    for t in json.loads(qbit("torrents/info")):
        if t["category"] in ("radarr", "sonarr", "lidarr") and t["state"] == "forcedUP" and t["amount_left"] == 0:
            log("tirei o 'forçar início' (para poder sair do qBittorrent):", t["name"][:70])
            if not SIMULAR:
                qbit("torrents/setForceStart", f"hashes={t['hash']}&value=false")


def importar_bloqueados():
    for item in radarr("queue?pageSize=50")["records"]:
        msgs = " ".join(m for s in item.get("statusMessages", []) for m in s.get("messages", []))
        if item.get("trackedDownloadState") != "importBlocked" or BLOQUEIO not in msgs or not item.get("movieId"):
            continue
        cands = radarr("manualimport?" + q.urlencode({"downloadId": item["downloadId"], "movieId": item["movieId"],
                                                      "filterExistingFiles": "true"}))
        videos = [c for c in cands if c["path"].lower().endswith(VIDEO) and "sample" not in c["path"].lower()]
        if len(videos) != 1 or videos[0].get("rejections"):
            log("Radarr: importação travada, revise à mão:", item.get("title", "")[:70])
            continue
        v = videos[0]
        log("Radarr: importando o que estava travado:", item.get("title", "")[:70])
        if not SIMULAR:
            radarr("command", {"name": "ManualImport", "importMode": "auto", "files": [{
                "path": v["path"], "folderName": v.get("folderName"), "movieId": item["movieId"],
                "downloadId": item["downloadId"], "quality": v["quality"], "languages": v.get("languages") or [],
                "releaseGroup": v.get("releaseGroup") or "", "indexerFlags": 0}]})


def cadastrar_filmes_desconhecidos():
    for t in concluidos("radarr"):
        p = radarr("parse?" + q.urlencode({"title": t["name"]}))
        if p.get("movie"):
            continue
        info = p.get("parsedMovieInfo") or {}
        titulo, ano = info.get("movieTitle"), info.get("year")
        if not titulo or not ano:
            log("Radarr: download manual sem título/ano reconhecível, revise:", t["name"][:70])
            continue
        achados = [m for m in radarr("movie/lookup?" + q.urlencode({"term": f"{titulo} {ano}"}))
                   if m.get("year") == ano and normal(titulo) in nomes(m)]
        if len(achados) != 1:
            log(f"Radarr: download manual ambíguo ({len(achados)} filmes parecidos), revise:", t["name"][:70])
            continue
        m = achados[0]
        if m.get("id"):
            continue
        log("Radarr: cadastrei o filme baixado à mão:", m["title"], m["year"])
        if not SIMULAR:
            m.update(qualityProfileId=perfil_id("radarr"), rootFolderPath=FILMES, monitored=True,
                     minimumAvailability="released", addOptions={"searchForMovie": False})
            radarr("movie", m)
            radarr("command", {"name": "RefreshMonitoredDownloads"})


def importar_episodios(t, serie):
    origem = (t.get("content_path") or "").rstrip("/")
    if not origem.startswith("/data/"):
        log("Sonarr: torrent fora de /data, revise:", t["name"][:60])
        return
    if os.path.isfile(origem):
        arquivos = [origem]
    else:
        arquivos = sorted(os.path.join(r, f) for r, _, fs in os.walk(origem) for f in fs)
    arquivos = [a for a in arquivos if a.lower().endswith(VIDEO) and "sample" not in a.lower()]
    por_numero = {(e["seasonNumber"], e["episodeNumber"]): e for e in sonarr("episode?" + q.urlencode({"seriesId": serie["id"]}))}
    lote, faltam = [], []
    for a in arquivos:
        nome = os.path.basename(a)
        info = sonarr("parse?" + q.urlencode({"title": nome})).get("parsedEpisodeInfo") or {}
        if not info.get("episodeNumbers") and len(arquivos) == 1:
            info = sonarr("parse?" + q.urlencode({"title": t["name"]})).get("parsedEpisodeInfo") or {}
        eps = [por_numero.get((info.get("seasonNumber"), n)) for n in info.get("episodeNumbers") or []]
        if not eps or None in eps:
            faltam.append(nome)
            continue
        lote.append((a, eps, info))
    if not lote or faltam:
        log("Sonarr: não consegui importar, revise:", t["name"][:60], "->", serie["title"], "| sem episódio:", faltam[:3] or "nenhum vídeo")
        return
    log("Sonarr: importando episódios baixados à mão:", serie["title"],
        ",".join(f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}" for _, eps, _ in lote for e in eps))
    if not SIMULAR:
        sonarr("command", {"name": "ManualImport", "importMode": "auto", "files": [{
            "path": a, "seriesId": serie["id"], "episodeIds": [e["id"] for e in eps],
            "downloadId": t["hash"].upper(), "quality": info["quality"], "languages": info.get("languages") or [],
            "releaseGroup": info.get("releaseGroup") or "", "indexerFlags": 0} for a, eps, info in lote]})


def cadastrar_series_desconhecidas():
    for t in concluidos("sonarr"):
        p = sonarr("parse?" + q.urlencode({"title": t["name"]}))
        if p.get("series"):
            continue
        info = p.get("parsedEpisodeInfo") or {}
        titulo = info.get("seriesTitle")
        ano = (info.get("seriesTitleInfo") or {}).get("year") or 0
        if not titulo:
            log("Sonarr: download manual sem nome de série reconhecível, revise:", t["name"][:70])
            continue
        res = sonarr("series/lookup?" + q.urlencode({"term": titulo}))
        exatos = [s for s in res if normal(titulo) in nomes(s) and (not ano or s.get("year") == ano)]
        parecidos = [s for s in res if normal(s.get("title")).startswith(normal(titulo))]
        if len(exatos) == 1 and not exatos[0].get("id") and not ano and len(parecidos) > 1:
            exatos = parecidos  # "The Office" também casa com "The Office (US)": ambíguo
        if len(exatos) == 1:
            s = exatos[0]
        elif not exatos and res and res[0].get("id"):
            s = res[0]  # nome em português de série já cadastrada
        else:
            log(f"Sonarr: download manual ambíguo ({len(exatos)} séries), revise:", t["name"][:70])
            continue
        if s.get("id"):
            importar_episodios(t, s)
            continue
        log("Sonarr: cadastrei a série baixada à mão:", s["title"], s.get("year"))
        if not SIMULAR:
            s.update(qualityProfileId=perfil_id("sonarr"), rootFolderPath=SERIES, monitored=True, seasonFolder=True,
                     addOptions={"monitor": "future", "searchForMissingEpisodes": False,
                                 "searchForCutoffUnmetEpisodes": False})
            sonarr("series", s)
            sonarr("command", {"name": "RefreshMonitoredDownloads"})


def bloquear_resolucao_falsa(app, campo):
    desde = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=JANELA_LOOP_H)
    hist = arr(app, "history?" + q.urlencode({"pageSize": 300, "sortKey": "date", "sortDirection": "descending"}))["records"]
    tratados = set()
    for imp in hist:
        if imp["eventType"] != "downloadFolderImported" or not imp.get("downloadId"):
            continue
        if dt.datetime.fromisoformat(imp["date"].replace("Z", "+00:00")) < desde:
            continue
        mesmos = [h for h in hist if h.get("downloadId") == imp["downloadId"] and h.get(campo) == imp.get(campo)]
        grabs = [h for h in mesmos if h["eventType"] == "grabbed" and h["date"] <= imp["date"]]
        if not grabs:
            continue
        grab = grabs[0]
        if grab["id"] in tratados or any(h["eventType"] == "downloadFailed" and h["date"] >= grab["date"] for h in mesmos):
            continue
        prometido = grab["quality"]["quality"].get("resolution") or 0
        real = imp["quality"]["quality"].get("resolution") or 0
        if real and prometido > real:
            tratados.add(grab["id"])
            log(f"{app.capitalize()}: versão dizia {prometido}p mas o arquivo é {real}p; bloqueada para não repetir:",
                grab["sourceTitle"][:70])
            if not SIMULAR:
                arr(app, f"history/failed/{grab['id']}", {})


def lidarr_busca_espacada():
    ocupado = [c for c in arr("lidarr", "command") if c["status"] in ("queued", "started")
               and c["name"] in ("AlbumSearch", "MissingAlbumSearch")]
    if ocupado:
        return
    faltando = arr("lidarr", "wanted/missing?pageSize=2000&monitored=true&includeArtist=true")["records"]
    if ENV.get("LIDARR_SEM_AO_VIVO", "sim").lower() in SIM:
        shows = [a for a in faltando if SHOW.search(a["title"])]
        if shows:
            log("Lidarr: desmarquei (ao vivo):", "; ".join(f"{a['artist']['artistName']} - {a['title']}" for a in shows[:10]),
                f"(+{len(shows) - 10})" if len(shows) > 10 else "")
            if not SIMULAR:
                arr("lidarr", "album/monitor", {"albumIds": [a["id"] for a in shows], "monitored": False}, "PUT")
            faltando = [a for a in faltando if a not in shows]
    agora = dt.datetime.now(dt.timezone.utc)

    def ultima(a):
        t = a.get("lastSearchTime")
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    candidatos = sorted((a for a in faltando if agora - ultima(a) > dt.timedelta(hours=24)), key=ultima)
    if candidatos:
        a = candidatos[0]
        log(f"Lidarr: procurando {a['artist']['artistName']} - {a['title']} ({len(candidatos) - 1} na fila)")
        if not SIMULAR:
            arr("lidarr", "command", {"name": "AlbumSearch", "albumIds": [a["id"]]})


TAREFAS = [tirar_forcado, importar_bloqueados, cadastrar_filmes_desconhecidos, cadastrar_series_desconhecidas,
           lambda: bloquear_resolucao_falsa("radarr", "movieId"), lambda: bloquear_resolucao_falsa("sonarr", "seriesId")]


def rodada(com_lidarr):
    tarefas = TAREFAS + ([lidarr_busca_espacada] if com_lidarr else [])
    for t in tarefas:
        try:
            t()
        except Exception as e:  # um app fora do ar não impede o resto
            log(f"erro em {getattr(t, '__name__', 'tarefa')}: {e}")


if __name__ == "__main__":
    busca_lidarr = ENV.get("LIDARR_BUSCA_ESPACADA", "sim").lower() in SIM
    if UMA_VEZ:
        rodada(busca_lidarr)
        sys.exit(0)
    log("manutenção iniciada (a cada 5 minutos)" + (" — simulação" if SIMULAR else ""))
    time.sleep(120)  # deixa os apps subirem
    ultima_lidarr = 0.0
    while True:
        agora = time.time()
        vez_lidarr = busca_lidarr and agora - ultima_lidarr >= INTERVALO_LIDARR
        rodada(vez_lidarr)
        if vez_lidarr:
            ultima_lidarr = agora
        time.sleep(INTERVALO)
