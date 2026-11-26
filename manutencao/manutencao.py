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
6. Busca de lançamentos (BUSCA_PERIODICA): o RSS só vê o que sai na hora, e a versão dublada
   costuma sair dias depois. A cada 6 h procura episódios exibidos nos últimos 30 dias e filmes
   lançados nos últimos 90 dias que ainda faltam; 1 vez por dia procura tudo o que falta.
7. Limpeza pelo Jellyfin (LIMPEZA_JELLYFIN, de hora em hora): filme/série que você apagou pelo
   Jellyfin sai do Radarr/Sonarr, para não ser baixado de novo. Só remove quando TUDO vale: não
   está mais no Jellyfin, tem 0 arquivos, o histórico mostra que o arquivo foi apagado, está
   desmonitorado e foi adicionado há mais de 24 h. Não roda se o Jellyfin estiver fora, com a
   biblioteca quase vazia ou com a pasta inacessível; no máximo 3 por vez. Nunca apaga arquivos.

Ver o que ela fez:  docker compose logs manutencao
Simular sem mudar nada:  docker compose run --rm manutencao python /app/manutencao.py --simular --uma-vez
Só uma tarefa:  ... python /app/manutencao.py --simular --uma-vez --so limpeza_jellyfin
"""
import datetime as dt, http.cookiejar, json, os, re, sys, time, unicodedata
import urllib.parse as q, urllib.request as u

SIMULAR = "--simular" in sys.argv
UMA_VEZ = "--uma-vez" in sys.argv
SO = sys.argv[sys.argv.index("--so") + 1].split(",") if "--so" in sys.argv[:-1] else None
ENV = os.environ
SIM = ("sim", "s", "yes", "y", "true", "1")
INTERVALO = 300
INTERVALO_LIDARR = 600
DIAS_EPISODIO, DIAS_FILME = 30, 90       # o que conta como "lançamento" na busca a cada 6 h
MAX_REMOCOES = 3                         # limpeza pelo Jellyfin: mais que isso = suspeito, não mexe
MIN_ITENS_JELLYFIN = 5                   # menos que isso = biblioteca não carregou / disco fora
JELLYFIN = "http://jellyfin:8096"
JF_AUT = 'MediaBrowser Client="arr-dublado", Device="manutencao", DeviceId="arr-dublado-manutencao", Version="1.0"'
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


_jf_token = None


def jellyfin(caminho):
    """Entra no Jellyfin com o USUARIO/SENHA do .env (o mesmo de todos os painéis).
    Se você mudou a senha só no Jellyfin, coloque uma chave de API dele em JELLYFIN_CHAVE no .env."""
    global _jf_token
    if _jf_token is None and ENV.get("JELLYFIN_CHAVE"):
        _jf_token = ENV["JELLYFIN_CHAVE"]
    if _jf_token is None:
        r = u.Request(JELLYFIN + "/Users/AuthenticateByName", method="POST",
                      data=json.dumps({"Username": ENV.get("USUARIO", "admin"), "Pw": ENV.get("SENHA", "")}).encode(),
                      headers={"Authorization": JF_AUT, "Content-Type": "application/json"})
        _jf_token = json.loads(u.urlopen(r, timeout=30).read())["AccessToken"]
    r = u.Request(JELLYFIN + caminho, headers={"Authorization": JF_AUT + f', Token="{_jf_token}"'})
    try:
        return json.loads(u.urlopen(r, timeout=60).read())
    except u.HTTPError as e:
        if e.code == 401:  # sessão expirou ou senha mudou
            _jf_token = None
        raise


def data(txt):
    return dt.datetime.fromisoformat(txt.replace("Z", "+00:00")) if txt else None


def buscando(app, nomes):
    return any(c["status"] in ("queued", "started") and c["name"] in nomes for c in arr(app, "command"))


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


def busca_recentes():
    agora = dt.datetime.now(dt.timezone.utc)
    if not buscando("sonarr", ("EpisodeSearch", "MissingEpisodeSearch", "SeriesSearch")):
        faltam = arr("sonarr", "wanted/missing?pageSize=1000&monitored=true&includeSeries=true")["records"]
        eps = [e for e in faltam if (d := data(e.get("airDateUtc"))) and agora - dt.timedelta(days=DIAS_EPISODIO) <= d <= agora]
        if eps:
            log(f"Sonarr: procurando {len(eps)} episódio(s) recente(s):",
                ", ".join(f"{e['series']['title']} {e['seasonNumber']}x{e['episodeNumber']:02d}" for e in eps[:10]))
            if not SIMULAR:
                arr("sonarr", "command", {"name": "EpisodeSearch", "episodeIds": [e["id"] for e in eps]})
    if not buscando("radarr", ("MoviesSearch", "MissingMoviesSearch")):
        filmes = []
        for m in arr("radarr", "movie"):
            if not m.get("monitored") or m.get("hasFile") or not m.get("isAvailable"):
                continue
            datas = [d for k in ("digitalRelease", "physicalRelease", "inCinemas") if (d := data(m.get(k))) and d <= agora]
            if datas and agora - max(datas) <= dt.timedelta(days=DIAS_FILME):
                filmes.append(m)
        if filmes:
            log(f"Radarr: procurando {len(filmes)} filme(s) recente(s):", ", ".join(m["title"] for m in filmes[:10]))
            if not SIMULAR:
                arr("radarr", "command", {"name": "MoviesSearch", "movieIds": [m["id"] for m in filmes]})


def busca_tudo():
    if not buscando("sonarr", ("EpisodeSearch", "MissingEpisodeSearch")):
        log("Sonarr: procurando todos os episódios monitorados que faltam")
        if not SIMULAR:
            arr("sonarr", "command", {"name": "MissingEpisodeSearch", "monitored": True})
    if not buscando("radarr", ("MoviesSearch", "MissingMoviesSearch")):
        log("Radarr: procurando todos os filmes monitorados que faltam")
        if not SIMULAR:
            arr("radarr", "command", {"name": "MissingMoviesSearch", "filterKey": "monitored", "filterValue": "true"})


def ids_jellyfin(tipo):
    itens = jellyfin(f"/Items?Recursive=true&IncludeItemTypes={tipo}&Fields=ProviderIds")["Items"]
    return len(itens), {(k.lower(), str(v).lower()) for i in itens for k, v in (i.get("ProviderIds") or {}).items() if v}


def apagado_de_verdade(h, evento):
    """Arquivo que sumiu do disco (apagado pelo Jellyfin) ou apagado à mão; troca por versão melhor não conta."""
    return h["eventType"] == evento and (h.get("data") or {}).get("reason") in ("MissingFromDisk", "Manual")


def limpeza_jellyfin():
    try:
        n_series, jf_series = ids_jellyfin("Series")
        n_filmes, jf_filmes = ids_jellyfin("Movie")
    except Exception as e:
        log("Limpeza: Jellyfin fora, nada feito:", e)
        return
    if n_series + n_filmes < MIN_ITENS_JELLYFIN:
        log(f"Limpeza: Jellyfin com só {n_series + n_filmes} itens (fora ou sem disco?), nada feito")
        return
    for app in ("sonarr", "radarr"):
        raizes = arr(app, "rootfolder")
        if not raizes or not all(r.get("accessible") for r in raizes):
            log(f"Limpeza: pasta do {app.capitalize()} inacessível, nada feito")
            return
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    candidatos = []
    for s in arr("sonarr", "series"):
        if (s.get("statistics") or {}).get("episodeFileCount", 0) > 0 or (data(s.get("added")) or limite) > limite:
            continue
        if {("tvdb", str(s.get("tvdbId"))), ("tmdb", str(s.get("tmdbId"))), ("imdb", str(s.get("imdbId") or "").lower())} & jf_series:
            continue
        if not any(apagado_de_verdade(h, "episodeFileDeleted") for h in arr("sonarr", f"history/series?seriesId={s['id']}")):
            continue
        # o Sonarr desmonitora sozinho o episódio apagado; se você religou, a limpeza respeita
        if s["monitored"] and any(e["monitored"] for e in arr("sonarr", f"episode?seriesId={s['id']}")):
            continue
        candidatos.append(("sonarr", s["title"], f"series/{s['id']}?deleteFiles=false&addImportListExclusion=false"))
    for m in arr("radarr", "movie"):
        if m.get("hasFile") or m["monitored"] or (data(m.get("added")) or limite) > limite:
            continue
        if {("tmdb", str(m.get("tmdbId"))), ("imdb", str(m.get("imdbId") or "").lower())} & jf_filmes:
            continue
        if not any(apagado_de_verdade(h, "movieFileDeleted") for h in arr("radarr", f"history/movie?movieId={m['id']}")):
            continue
        candidatos.append(("radarr", m["title"], f"movie/{m['id']}?deleteFiles=false&addImportExclusion=false"))
    if len(candidatos) > MAX_REMOCOES:
        log(f"Limpeza: {len(candidatos)} para remover (mais que {MAX_REMOCOES}), parece erro, nada feito:",
            "; ".join(c[1] for c in candidatos[:10]))
        return
    for app, titulo, caminho in candidatos:
        log(f"{app.capitalize()}: removi {titulo} (você apagou pelo Jellyfin; arquivos não foram tocados)")
        if not SIMULAR:
            arr(app, caminho, metodo="DELETE")


TAREFAS = [tirar_forcado, importar_bloqueados, cadastrar_filmes_desconhecidos, cadastrar_series_desconhecidas,
           lambda: bloquear_resolucao_falsa("radarr", "movieId"), lambda: bloquear_resolucao_falsa("sonarr", "seriesId")]
# tarefas com intervalo próprio: (função, intervalo em s, variável do .env, espera antes da 1ª vez)
PERIODICAS = [(lidarr_busca_espacada, INTERVALO_LIDARR, "LIDARR_BUSCA_ESPACADA", 0),
              (busca_recentes, 6 * 3600, "BUSCA_PERIODICA", 0),
              (busca_tudo, 24 * 3600, "BUSCA_PERIODICA", 3600),
              (limpeza_jellyfin, 3600, "LIMPEZA_JELLYFIN", 0)]


def ligada(variavel):
    return ENV.get(variavel, "sim").lower() in SIM


def rodar(t):
    try:
        t()
    except Exception as e:  # um app fora do ar não impede o resto
        log(f"erro em {getattr(t, '__name__', 'tarefa')}: {e}")


if __name__ == "__main__":
    if UMA_VEZ:
        todas = TAREFAS + [f for f, _, var, _ in PERIODICAS if ligada(var)]
        for t in todas:
            if SO is None or getattr(t, "__name__", "") in SO:
                rodar(t)
        sys.exit(0)
    log("manutenção iniciada (a cada 5 minutos)" + (" — simulação" if SIMULAR else ""))
    time.sleep(120)  # deixa os apps subirem
    inicio = time.time()
    proxima = {f: inicio + espera for f, _, _, espera in PERIODICAS}
    while True:
        for t in TAREFAS:
            rodar(t)
        for f, intervalo, var, _ in PERIODICAS:
            if ligada(var) and time.time() >= proxima[f]:
                rodar(f)
                proxima[f] = time.time() + intervalo
        time.sleep(INTERVALO)
