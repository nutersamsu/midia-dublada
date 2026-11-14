#!/usr/bin/env python3
"""Configura sozinho todos os apps da pilha arr-dublado-ptbr, pela API de cada um.

Roda dentro de um container (docker compose run --rm configurador ...), chamado pelo instalar.sh:
  preparar    antes de subir os apps: cria as pastas e a senha do qBittorrent
  configurar  com os apps no ar: liga tudo (pode rodar quantas vezes quiser; só ajusta o que falta)

Só usa a biblioteca padrão do Python. Os nomes dos containers (radarr, sonarr...) são os
nomes dos serviços no docker-compose.yml.
"""
import base64, hashlib, http.cookiejar, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
CONFIGS = "/config-apps"
DADOS = "/data"
ENV = os.environ
USUARIO = ENV.get("USUARIO", "admin")
SENHA = ENV.get("SENHA", "")
PUID, PGID = int(ENV.get("PUID", "1000")), int(ENV.get("PGID", "1000"))
PORTA_TORRENT = int(ENV.get("PORTA_TORRENT", "6881"))
PORTA_QBIT = int(ENV.get("PORTA_QBITTORRENT", "8080"))  # dentro e fora iguais
QBIT = f"http://qbittorrent:{PORTA_QBIT}"
SIM = ("sim", "s", "yes", "y", "true", "1")

PASTAS = ["downloads/incompletos", "downloads/radarr", "downloads/sonarr", "downloads/lidarr",
          "downloads/manual", "midia/filmes", "midia/series", "midia/musica"]
FILMES, SERIES, MUSICA = "/data/midia/filmes", "/data/midia/series", "/data/midia/musica"
NOME_CHAVE_JELLYFIN = "arr-dublado"
REGRAS = json.load(open(os.path.join(AQUI, "formatos-ptbr.json"), encoding="utf-8"))
PERFIL = REGRAS["perfil"]["nome"]

avisos = []


def ok(msg):
    print(f"  ✔ {msg}", flush=True)


def etapa(msg):
    print(f"\n▶ {msg}", flush=True)


def aviso(msg):
    avisos.append(msg)
    print(f"  ⚠ {msg}", flush=True)


class ErroHTTP(Exception):
    def __init__(self, codigo, corpo, url):
        super().__init__(f"HTTP {codigo} em {url}: {corpo[:400]}")
        self.codigo, self.corpo = codigo, corpo


class Cliente:
    """HTTP simples com JSON, cabeçalhos fixos e cookies."""

    def __init__(self, base, cabecalhos=None):
        self.base = base.rstrip("/") + "/"
        self.cab = dict(cabecalhos or {})
        self.abridor = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def pedir(self, metodo, caminho, corpo=None, formulario=None, cab=None, tempo=60):
        url = self.base + caminho.lstrip("/")
        h = dict(self.cab, **(cab or {}))
        dados = None
        if formulario is not None:
            dados = urllib.parse.urlencode(formulario).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif corpo is not None:
            dados = json.dumps(corpo).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=dados, method=metodo, headers=h)
        try:
            with self.abridor.open(req, timeout=tempo) as r:
                txt = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ErroHTTP(e.code, e.read().decode("utf-8", "replace"), url) from None
        try:
            return json.loads(txt) if txt.strip() else None
        except ValueError:
            return txt

    def get(self, c, **k):
        return self.pedir("GET", c, **k)

    def post(self, c, corpo=None, **k):
        return self.pedir("POST", c, corpo, **k)

    def put(self, c, corpo=None, **k):
        return self.pedir("PUT", c, corpo, **k)


def esperar(nome, teste, limite=600):
    """Espera o app responder (na 1ª vez alguns demoram para criar o banco)."""
    inicio, ultimo = time.time(), None
    while time.time() - inicio < limite:
        try:
            r = teste()
            if r:
                return r
        except Exception as e:  # ainda subindo
            ultimo = e
        time.sleep(3)
    raise SystemExit(f"\n✖ {nome} não respondeu em {limite // 60} minutos ({ultimo}).\n"
                     f"  Veja o motivo com:  docker compose logs {nome.lower()}")


def chave_api(app):
    caminho = os.path.join(CONFIGS, app, "config.xml")
    def ler():
        if os.path.exists(caminho):
            m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(caminho, encoding="utf-8").read())
            return m and m.group(1)
    return esperar(app, ler)


def campos(item, valores):
    """Preenche os 'fields' de um item de schema do *arr pelo nome."""
    for f in item.get("fields", []):
        if f["name"] in valores:
            f["value"] = valores[f["name"]]
    return item


def do_schema(cli, caminho, implementacao):
    for s in cli.get(caminho):
        if s.get("implementation") == implementacao:
            return s
    return None


def salvar(cli, caminho, item, existente=None):
    """Cria ou atualiza (PUT) um item, mantendo o id."""
    if existente:
        item["id"] = existente["id"]
        return cli.put(f"{caminho}/{existente['id']}", item)
    return cli.post(caminho, item)


# ---------------------------------------------------------------- preparar
def preparar():
    etapa("Criando as pastas")
    # só as pastas da estrutura (não mexe no que já existe dentro delas)
    for p in ["", "downloads", "midia"] + PASTAS:
        caminho = os.path.join(DADOS, p)
        os.makedirs(caminho, exist_ok=True)
        os.chown(caminho, PUID, PGID)
        os.chmod(caminho, 0o2775)
    ok(f"pastas criadas em {ENV.get('PASTA_DADOS', DADOS)} (dono {PUID}:{PGID})")

    for app, dono in [("jellyfin", (PUID, PGID)), ("jellyfin-cache", (PUID, PGID)), ("seerr", (1000, 1000))]:
        p = os.path.join(CONFIGS, app)
        os.makedirs(p, exist_ok=True)
        os.chown(p, *dono)

    etapa("Senha do qBittorrent")
    pasta = os.path.join(CONFIGS, "qbittorrent", "qBittorrent")
    os.makedirs(pasta, exist_ok=True)
    conf = os.path.join(pasta, "qBittorrent.conf")
    sal = os.urandom(16)
    hash_ = hashlib.pbkdf2_hmac("sha512", SENHA.encode(), sal, 100000, 64)
    linha_senha = 'WebUI\\Password_PBKDF2="@ByteArray(%s:%s)"' % (base64.b64encode(sal).decode(), base64.b64encode(hash_).decode())
    if os.path.exists(conf):
        txt = open(conf, encoding="utf-8").read()
        txt = re.sub(r"(?m)^WebUI\\Password_PBKDF2=.*$", lambda _: linha_senha, txt)
        txt = re.sub(r"(?m)^WebUI\\Username=.*$", lambda _: "WebUI\\Username=" + USUARIO, txt)
        if "WebUI\\Password_PBKDF2" not in txt:
            txt = txt.replace("[Preferences]", "[Preferences]\n" + linha_senha, 1)
        if "WebUI\\Username" not in txt:
            txt = txt.replace("[Preferences]", "[Preferences]\nWebUI\\Username=" + USUARIO, 1)
    else:
        txt = "\n".join([
            "[BitTorrent]",
            "Session\\DefaultSavePath=/data/downloads/manual",
            "Session\\TempPath=/data/downloads/incompletos",
            "Session\\TempPathEnabled=true",
            f"Session\\Port={PORTA_TORRENT}",
            "",
            "[LegalNotice]",
            "Accepted=true",
            "",
            "[Preferences]",
            "General\\Locale=pt_BR",
            f"WebUI\\Port={PORTA_QBIT}",
            "WebUI\\Username=" + USUARIO,
            linha_senha,
            ""])
    open(conf, "w", encoding="utf-8").write(txt)
    for raiz, _, arqs in os.walk(os.path.join(CONFIGS, "qbittorrent")):
        os.chown(raiz, PUID, PGID)
        for a in arqs:
            os.chown(os.path.join(raiz, a), PUID, PGID)
    ok(f"usuário {USUARIO} com a senha do instalador")


# ---------------------------------------------------------------- qBittorrent
def qbittorrent():
    etapa("qBittorrent")
    qb = Cliente(QBIT + "/api/v2", {"Referer": QBIT})
    def entrar():
        r = qb.post("auth/login", formulario={"username": USUARIO, "password": SENHA})
        if r == "Fails.":
            raise SystemExit("\n✖ O qBittorrent recusou o usuário/senha do .env.\n"
                             "  Rode de novo o ./instalar.sh (ele regrava a senha com o qBittorrent parado).")
        return True
    esperar("qbittorrent", entrar)

    semear = ENV.get("SEMEAR", "sim").lower() in SIM
    ratio = float(ENV.get("SEMEAR_RATIO", "1") or 1) if semear else 0
    minutos = int(float(ENV.get("SEMEAR_DIAS", "7") or 7) * 1440) if semear else 0
    prefs = {
        "locale": "pt_BR",
        "save_path": "/data/downloads/manual",
        "temp_path_enabled": True,
        "temp_path": "/data/downloads/incompletos",
        # modo automático: a categoria decide a pasta
        "auto_tmm_enabled": True,
        "torrent_changed_tmm_enabled": True,
        "save_path_changed_tmm_enabled": True,
        "category_changed_tmm_enabled": True,
        "use_category_paths_in_manual_mode": True,
        "listen_port": PORTA_TORRENT,
        "random_port": False,
        "upnp": False,
        # torrent falso de filme costuma trazer programa: nem baixa esses arquivos
        "excluded_file_names_enabled": True,
        "excluded_file_names": "*.exe\n*.scr\n*.bat\n*.cmd\n*.com\n*.pif\n*.lnk\n*.vbs\n*.msi\n*.jar\n*.ps1",
        # limite de compartilhamento: ao atingir, o torrent para e o Radarr/Sonarr/Lidarr o removem
        "max_ratio_enabled": True,
        "max_ratio": ratio,
        "max_seeding_time_enabled": True,
        "max_seeding_time": minutos,
        "max_ratio_act": 0,
    }
    qb.post("app/setPreferences", formulario={"json": json.dumps(prefs)})
    ok("pastas, idioma, porta e bloqueio de .exe/.bat")
    ok(f"compartilhar depois de baixar: até ratio {ratio:g} ou {minutos // 1440} dia(s)" if semear
       else "compartilhar depois de baixar: NÃO (para assim que termina)")

    existentes = qb.get("torrents/categories") or {}
    for cat in ("radarr", "sonarr", "lidarr"):
        caminho = f"/data/downloads/{cat}"
        acao = "torrents/editCategory" if cat in existentes else "torrents/createCategory"
        qb.post(acao, formulario={"category": cat, "savePath": caminho})
    ok("categorias radarr, sonarr e lidarr")


# ---------------------------------------------------------------- *arr
class Arr(Cliente):
    def __init__(self, nome, porta, versao):
        self.nome, self.porta = nome, porta
        self.chave = chave_api(nome)
        super().__init__(f"http://{nome}:{porta}/api/{versao}", {"X-Api-Key": self.chave})
        esperar(nome, lambda: self.get("system/status"))


def login_painel(app):
    cfg = app.get("config/host")
    mudou = (cfg.get("authenticationMethod") != "forms" or cfg.get("username") != USUARIO
             or cfg.get("authenticationRequired") != "enabled")
    cfg.update(authenticationMethod="forms", authenticationRequired="enabled",
               username=USUARIO, password=SENHA, passwordConfirmation=SENHA)
    app.put(f"config/host/{cfg['id']}", cfg)
    ok("login do painel" + (" criado" if mudou else " conferido"))


def idioma_ptbr(app):
    try:
        for l in app.get("language"):
            if l["name"] in ("Portuguese (Brazil)", "Portuguese (BR)"):
                return l["id"]
    except ErroHTTP:
        pass
    return None


def interface_ptbr(app, extra=None):
    cfg = app.get("config/ui")
    lid = idioma_ptbr(app)
    if lid and "uiLanguage" in cfg:
        cfg["uiLanguage"] = lid
    for k, v in (extra or {}).items():
        if k in cfg:
            cfg[k] = lid if v == "ptbr" else v
    app.put(f"config/ui/{cfg.get('id', 1)}", cfg)
    ok("interface em português")


def pasta_raiz(app, caminho, extra=None):
    if any(r["path"].rstrip("/") == caminho for r in app.get("rootfolder")):
        ok(f"pasta {caminho} já cadastrada")
        return
    app.post("rootfolder", dict({"path": caminho}, **(extra or {})))
    ok(f"pasta {caminho} cadastrada")


def cliente_qbit(app, categoria):
    item = do_schema(app, "downloadclient/schema", "QBittorrent")
    valores = {"host": "qbittorrent", "port": PORTA_QBIT, "useSsl": False, "username": USUARIO, "password": SENHA}
    for f in item["fields"]:
        if f["name"].endswith("Category") and "Imported" not in f["name"]:
            valores[f["name"]] = categoria
    campos(item, valores)
    item.update(name="qBittorrent", enable=True, priority=1, removeCompletedDownloads=True,
                removeFailedDownloads=True, tags=[])
    existente = next((c for c in app.get("downloadclient") if c["implementation"] == "QBittorrent"), None)
    salvar(app, "downloadclient", item, existente)
    ok(f"qBittorrent ligado (categoria {categoria})")


def formatos_ptbr(app):
    """Custom formats do filtro de dublado + perfil de qualidade."""
    idiomas = {l["name"]: l["id"] for l in app.get("language")}
    atuais = {c["name"]: c for c in app.get("customformat")}
    for f in REGRAS["formatos"]:
        specs = []
        for nome_idioma in f.get("idiomas", []):
            if nome_idioma not in idiomas:
                aviso(f"{app.nome}: idioma {nome_idioma!r} não existe nesta versão")
                continue
            specs.append({"name": nome_idioma, "implementation": "LanguageSpecification", "negate": False,
                          "required": False, "fields": [{"name": "value", "value": idiomas[nome_idioma]}]})
        if f.get("regex"):
            specs.append({"name": f["nome"], "implementation": "ReleaseTitleSpecification", "negate": False,
                          "required": False, "fields": [{"name": "value", "value": f["regex"]}]})
        salvar(app, "customformat", {"name": f["nome"], "includeCustomFormatWhenRenaming": False,
                                     "specifications": specs}, atuais.get(f["nome"]))
    ok(f"{len(REGRAS['formatos'])} regras de dublado PT-BR")

    pontos = {f["nome"]: f["pontos"] for f in REGRAS["formatos"]}
    perfis = {p["name"]: p for p in app.get("qualityprofile")}
    perfil = perfis.get(PERFIL) or app.get("qualityprofile/schema")
    queridas = set(REGRAS["perfil"]["qualidades"])
    alvo = None
    for it in perfil["items"]:
        nome = it["quality"]["name"] if it.get("quality") else it.get("name")
        it["allowed"] = nome in queridas
        for sub in it.get("items", []):
            sub["allowed"] = it["allowed"]
        if it.get("quality") and nome == REGRAS["perfil"]["qualidade_alvo"]:
            alvo = it["quality"]["id"]
        for sub in it.get("items", []):
            if sub.get("quality", {}).get("name") == REGRAS["perfil"]["qualidade_alvo"]:
                alvo = it.get("id") or sub["quality"]["id"]
    ids_formatos = {c["name"]: c["id"] for c in app.get("customformat")}
    perfil["formatItems"] = [{"format": cid, "name": n, "score": pontos.get(n, 0)} for n, cid in ids_formatos.items()]
    perfil.update(name=PERFIL, upgradeAllowed=True, cutoff=alvo,
                  minFormatScore=REGRAS["perfil"]["minimo"],
                  cutoffFormatScore=REGRAS["perfil"]["pontuacao_para_parar"], minUpgradeFormatScore=1)
    if "language" in perfil:  # Radarr: o perfil não pode filtrar idioma (senão só aceita inglês)
        perfil["language"] = next((l for l in app.get("language") if l["id"] == -1), {"id": -1, "name": "Any"})
    salvar(app, "qualityprofile", perfil, perfis.get(PERFIL))
    ok(f"perfil {PERFIL!r} (mínimo {REGRAS['perfil']['minimo']} pontos)")
    return next(p["id"] for p in app.get("qualityprofile") if p["name"] == PERFIL)


def rss_15min(app):
    try:
        cfg = app.get("config/indexer")
        cfg["rssSyncInterval"] = 15
        app.put(f"config/indexer/{cfg['id']}", cfg)
        ok("procura novidades (RSS) a cada 15 minutos")
    except ErroHTTP as e:
        aviso(f"{app.nome}: não ajustei o RSS ({e.codigo})")


def nomes_radarr(app):
    cfg = app.get("config/naming")
    cfg.update(renameMovies=True, replaceIllegalCharacters=True, colonReplacementFormat="smart",
               standardMovieFormat="{Movie Title:pt-BR} ({Release Year}) {Quality Full}",
               movieFolderFormat="{Movie Title:pt-BR} ({Release Year})")
    app.put(f"config/naming/{cfg['id']}", cfg)
    ok("nomes de pastas e arquivos em português")


def nomes_sonarr(app):
    cfg = app.get("config/naming")
    cfg.update(renameEpisodes=True, replaceIllegalCharacters=True,
               standardEpisodeFormat="{Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}",
               seriesFolderFormat="{Series Title}", seasonFolderFormat="Temporada {season}")
    app.put(f"config/naming/{cfg['id']}", cfg)
    ok("nomes de pastas e arquivos organizados (Temporada 1, 2...)")


def radarr():
    etapa("Radarr (filmes)")
    app = Arr("radarr", 7878, "v3")
    login_painel(app)
    interface_ptbr(app, {"movieInfoLanguage": "ptbr"})
    nomes_radarr(app)
    perfil = formatos_ptbr(app)
    pasta_raiz(app, FILMES)
    cliente_qbit(app, "radarr")
    rss_15min(app)
    return app, perfil


def sonarr():
    etapa("Sonarr (séries)")
    app = Arr("sonarr", 8989, "v3")
    login_painel(app)
    interface_ptbr(app)
    nomes_sonarr(app)
    perfil = formatos_ptbr(app)
    pasta_raiz(app, SERIES)
    cliente_qbit(app, "sonarr")
    rss_15min(app)
    return app, perfil


def lidarr():
    etapa("Lidarr (música)")
    app = Arr("lidarr", 8686, "v1")
    login_painel(app)
    interface_ptbr(app)

    nome_meta = "Álbuns de estúdio"
    metas = {m["name"]: m for m in app.get("metadataprofile")}
    meta = metas.get(nome_meta) or app.get("metadataprofile/schema")
    for grupo, permitidos in (("primaryAlbumTypes", {"Album", "EP"}), ("secondaryAlbumTypes", {"Studio"}),
                              ("releaseStatuses", {"Official"})):
        for t in meta.get(grupo, []):
            obj = t.get("albumType") or t.get("releaseStatus") or {}
            t["allowed"] = obj.get("name") in permitidos
    meta["name"] = nome_meta
    salvar(app, "metadataprofile", meta, metas.get(nome_meta))
    meta_id = next(m["id"] for m in app.get("metadataprofile") if m["name"] == nome_meta)
    ok("só álbuns e EPs de estúdio (sem singles, coletâneas e ao vivo)")

    perfis = {p["name"]: p["id"] for p in app.get("qualityprofile")}
    perfil_id = perfis.get("Standard") or next(iter(perfis.values()))
    pasta_raiz(app, MUSICA, {"name": "Música", "defaultQualityProfileId": perfil_id,
                             "defaultMetadataProfileId": meta_id, "defaultMonitorOption": "all",
                             "defaultNewItemMonitorOption": "all", "defaultTags": []})

    ignorar = ["/\\blive\\b/i", "/ao vivo/i", "/unplugged/i"]
    if not any(set(ignorar) <= set(r.get("ignored") or []) for r in app.get("releaseprofile")):
        app.post("releaseprofile", {"enabled": True, "required": [], "ignored": ignorar, "indexerId": 0, "tags": []})
    ok("ignora versões ao vivo/unplugged")

    cfg = app.get("config/naming")
    cfg["renameTracks"] = True
    app.put(f"config/naming/{cfg['id']}", cfg)
    cliente_qbit(app, "lidarr")
    return app


def prowlarr(apps):
    etapa("Prowlarr (indexadores)")
    app = Arr("prowlarr", 9696, "v1")
    login_painel(app)
    try:
        interface_ptbr(app)
    except ErroHTTP:
        pass
    atuais = {a["implementation"]: a for a in app.get("applications")}
    for alvo in apps:
        impl = alvo.nome.capitalize()
        item = do_schema(app, "applications/schema", impl)
        campos(item, {"prowlarrUrl": "http://prowlarr:9696", "baseUrl": f"http://{alvo.nome}:{alvo.porta}",
                      "apiKey": alvo.chave})
        item.update(name=impl, syncLevel="fullSync", tags=[])
        salvar(app, "applications", item, atuais.get(impl))
        ok(f"{impl} recebe os indexadores automaticamente")

    tags = {t["label"]: t["id"] for t in app.get("tag")}
    tag = tags.get("flaresolverr") or app.post("tag", {"label": "flaresolverr"})["id"]
    item = do_schema(app, "indexerProxy/schema", "FlareSolverr")
    campos(item, {"host": "http://flaresolverr:8191/", "requestTimeout": 60})
    item.update(name="FlareSolverr", tags=[tag])
    existente = next((p for p in app.get("indexerProxy") if p["implementation"] == "FlareSolverr"), None)
    salvar(app, "indexerProxy", item, existente)
    ok("FlareSolverr pronto (use a tag 'flaresolverr' nos indexadores com Cloudflare)")


# ---------------------------------------------------------------- Jellyfin
JF_AUT = 'MediaBrowser Client="arr-dublado", Device="configurador", DeviceId="arr-dublado-configurador", Version="1.0"'


def jellyfin():
    etapa("Jellyfin")
    jf = Cliente("http://jellyfin:8096", {"Authorization": JF_AUT})
    info = esperar("jellyfin", lambda: jf.get("System/Info/Public"))
    if not info.get("StartupWizardCompleted"):
        # o Jellyfin aceita a primeira configuração só depois de terminar de iniciar
        esperar("jellyfin", lambda: jf.get("Startup/Configuration") is not None)
        jf.post("Startup/Configuration", {"UICulture": "pt-BR", "MetadataCountryCode": "BR",
                                          "PreferredMetadataLanguage": "pt"})
        jf.get("Startup/User")
        jf.post("Startup/User", {"Name": USUARIO, "Password": SENHA})
        try:
            jf.post("Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        except ErroHTTP:
            pass
        jf.post("Startup/Complete")
        ok(f"configuração inicial feita (usuário {USUARIO})")
    try:
        sessao = jf.post("Users/AuthenticateByName", {"Username": USUARIO, "Pw": SENHA})
    except ErroHTTP as e:
        raise SystemExit(f"\n✖ O Jellyfin recusou o usuário {USUARIO} ({e.codigo}). Se você mudou a senha"
                         " pelo Jellyfin, coloque a mesma no .env (SENHA=) e rode de novo.")
    jf.cab["Authorization"] = JF_AUT + f', Token="{sessao["AccessToken"]}"'
    uid = sessao["User"]["Id"]

    existentes = {b["Name"]: b for b in jf.get("Library/VirtualFolders")}
    caminhos = {p for b in existentes.values() for p in b.get("Locations", [])}
    for nome, tipo, caminho in (("Filmes", "movies", FILMES), ("Séries", "tvshows", SERIES), ("Música", "music", MUSICA)):
        if caminho in caminhos or nome in existentes:
            continue
        opcoes = {"LibraryOptions": {"PreferredMetadataLanguage": "pt", "MetadataCountryCode": "BR",
                                     "EnableRealtimeMonitor": True, "PathInfos": [{"Path": caminho}]}}
        jf.post("Library/VirtualFolders?" + urllib.parse.urlencode(
            {"name": nome, "collectionType": tipo, "refreshLibrary": "false", "paths": caminho}), opcoes)
        ok(f"biblioteca {nome} -> {caminho}")
    ok("bibliotecas Filmes, Séries e Música")

    usuario = jf.get(f"Users/{uid}")
    cfg = usuario["Configuration"]
    cfg.update(AudioLanguagePreference="por", PlayDefaultAudioTrack=False,
               SubtitleLanguagePreference="por", SubtitleMode="Smart")
    try:
        jf.post(f"Users/Configuration?userId={uid}", cfg)
    except ErroHTTP:
        jf.post(f"Users/{uid}/Configuration", cfg)
    ok("áudio preferido: português")

    def chave():
        for k in (jf.get("Auth/Keys") or {}).get("Items", []):
            if k.get("AppName") == NOME_CHAVE_JELLYFIN:
                return k["AccessToken"]
    token = chave()
    if not token:
        jf.post("Auth/Keys?" + urllib.parse.urlencode({"app": NOME_CHAVE_JELLYFIN}))
        token = chave()
    ok("chave de API para avisar o Jellyfin de cada download novo")
    return token


def avisar_jellyfin(app, token):
    """Conexão 'Jellyfin/Emby' no *arr: atualiza a biblioteca a cada importação."""
    item = do_schema(app, "notification/schema", "MediaBrowser")
    if not item:
        ok("(este app não tem conexão com o Jellyfin; o Jellyfin acompanha a pasta sozinho)")
        return
    campos(item, {"host": "jellyfin", "port": 8096, "useSsl": False, "apiKey": token,
                  "notify": False, "updateLibrary": True})
    for k in list(item):
        if k.startswith("on") and item.get("supportsO" + k[1:]) and k not in (
                "onGrab", "onHealthIssue", "onHealthRestored", "onApplicationUpdate", "onManualInteractionRequired"):
            item[k] = True
    item.update(name="Jellyfin", tags=[])
    existente = next((n for n in app.get("notification") if n["implementation"] == "MediaBrowser"), None)
    salvar(app, "notification", item, existente)
    ok(f"{app.nome.capitalize()} avisa o Jellyfin quando chega arquivo novo")


# ---------------------------------------------------------------- Seerr
def seerr(rad, rad_perfil, son, son_perfil):
    etapa("Seerr (pedidos)")
    se = Cliente("http://seerr:5055/api/v1")
    publico = esperar("seerr", lambda: se.get("settings/public"))
    if not publico.get("initialized"):
        try:
            se.post("auth/jellyfin", {"username": USUARIO, "password": SENHA, "hostname": "jellyfin", "port": 8096,
                                      "useSsl": False, "urlBase": "", "email": f"{USUARIO}@casa.local",
                                      "serverType": 2})
        except ErroHTTP as e:
            if e.codigo != 500 or "already" not in e.corpo.lower():
                raise
        ok("Seerr ligado ao Jellyfin (entre no Seerr com o usuário do Jellyfin)")
    chave = json.load(open(os.path.join(CONFIGS, "seerr", "settings.json"), encoding="utf-8"))["main"]["apiKey"]
    se.cab["X-Api-Key"] = chave

    try:
        libs = se.post("settings/jellyfin/library/sync") or []
    except ErroHTTP:  # versões antigas (Jellyseerr)
        libs = se.get("settings/jellyfin/library?sync=true") or []
    for lib in libs:
        if lib.get("type") in ("movie", "show") and not lib.get("enabled"):
            try:
                se.put(f"settings/jellyfin/library/{lib['id']}", {"enabled": True})
            except ErroHTTP:
                se.get(f"settings/jellyfin/library?enable={lib['id']}")
    ok("bibliotecas Filmes e Séries do Jellyfin ativadas")

    se.post("settings/main", {"locale": "pt-BR", "region": "BR"})  # o Seerr junta com o que já existe

    comum = {"hostname": None, "useSsl": False, "baseUrl": "", "is4k": False, "isDefault": True,
             "externalUrl": "", "syncEnabled": True, "preventSearch": False, "tags": []}
    radarr_cfg = dict(comum, name="Radarr", hostname="radarr", port=7878, apiKey=rad.chave,
                      activeProfileId=rad_perfil, activeProfileName=PERFIL, activeDirectory=FILMES,
                      minimumAvailability="released")
    sonarr_cfg = dict(comum, name="Sonarr", hostname="sonarr", port=8989, apiKey=son.chave,
                      activeProfileId=son_perfil, activeProfileName=PERFIL, activeDirectory=SERIES,
                      activeAnimeProfileId=son_perfil, activeAnimeProfileName=PERFIL, activeAnimeDirectory=SERIES,
                      seriesType="standard", animeSeriesType="anime", enableSeasonFolders=True,
                      animeTags=[])
    for tipo, cfg in (("radarr", radarr_cfg), ("sonarr", sonarr_cfg)):
        atuais = se.get(f"settings/{tipo}") or []
        if atuais:
            sid = atuais[0]["id"]
            se.put(f"settings/{tipo}/{sid}", cfg)  # sem o campo "id" no corpo (senão o Seerr responde 400)
        else:
            se.post(f"settings/{tipo}", cfg)
        ok(f"{tipo.capitalize()} ligado (perfil {PERFIL})")

    if not publico.get("initialized"):
        se.post("settings/initialize")
    try:
        se.post("settings/jobs/jellyfin-full-scan/run")
    except ErroHTTP:
        pass
    ok("pronto para receber pedidos em português")


# ---------------------------------------------------------------- principal
def configurar():
    if len(SENHA) < 8:
        raise SystemExit("✖ A SENHA do .env precisa ter pelo menos 8 caracteres.")
    print("Configurando tudo. Na primeira vez pode levar alguns minutos enquanto os apps iniciam.")
    qbittorrent()
    rad, rad_perfil = radarr()
    son, son_perfil = sonarr()
    lid = lidarr()
    prowlarr([rad, son, lid])
    token = jellyfin()
    etapa("Avisos para o Jellyfin")
    for app in (rad, son, lid):
        try:
            avisar_jellyfin(app, token)
        except ErroHTTP as e:
            aviso(f"{app.nome}: conexão com o Jellyfin falhou ({e})")
    seerr(rad, rad_perfil, son, son_perfil)
    print("\n✔ Configuração concluída." + (f" {len(avisos)} aviso(s) acima." if avisos else ""))


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else "configurar"
    try:
        {"preparar": preparar, "configurar": configurar}[acao]()
    except ErroHTTP as e:
        raise SystemExit(f"\n✖ Erro inesperado: {e}\n  Rode o instalador de novo; se repetir, abra uma issue com esta mensagem.")
