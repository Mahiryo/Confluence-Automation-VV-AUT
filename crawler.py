from email.utils import parsedate_to_datetime
import csv
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urljoin, urlsplit

try:
    import requests
    import truststore
    from dotenv import dotenv_values
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
except ImportError:
    sys.exit("Esqueceu das dependências. Instale com: python -m pip install -r requirements.txt")


import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["local"]
mycol = mydb["logs_conflu_automation"]

# mydict = { "name": "John", "address": "Highway 37" }


CSV_ENTRADA = "entrada/apis.csv"
DELIMITADOR = ","
COLUNA_ID = "PAGE_ID"
PASTA_SAIDA = "saida"
ARQUIVO_ENV = ".env"
APENAS_VERIFICAR = False
PAGINA_TESTE = ""


INTERVALO_MINIMO = 3
TENTATIVAS = 3
MAX_RETRY_AFTER = 120
TIMEOUT = (10, 60)
TAMANHO_MAXIMO_MB = 50
CONSULTAS_TAREFA = 10
FALHAS_DE_REDE_SEGUIDAS = 3

TIPOS = {"API": "", "API-MTD": "", "MS": "MS", "MS-MTD": "MS", "APID": "APID", "APID-MTD": "APID"}
LIMITE_CAMINHO_WINDOWS = 260

log = logging.getLogger("crawler")

proxima_requisicao = 0.0
requisicoes = 0


def chave(nome):
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", nome) if not unicodedata.combining(c))
    return re.sub(r"[\s_-]+", "_", sem_acento.strip().upper()).strip("_")


def nome_pasta(texto):
    nome = unicodedata.normalize("NFC", texto).strip()
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", nome)
    nome = " ".join(nome.split())[:80].strip(" .")
    if not nome.strip("_."):
        return ""
    if nome.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                       *(f"LPT{i}" for i in range(1, 10))}:
        nome = "_" + nome
    return nome


def eh_metodo(texto):
    return re.match(r"\s*m[eé]todo", unicodedata.normalize("NFC", texto), re.IGNORECASE) is not None


def nome_arquivo(texto):
    texto = re.sub(r"(^|\s)/+", r"\1", texto.strip())
    return nome_pasta(re.sub(r"\s*/+\s*", "-", texto))


def destino_pdf(jornada, api, tipo, nome):

    pasta = PurePosixPath(nome_pasta(jornada), nome_pasta(api))
    subpasta = TIPOS[tipo]
    if subpasta:
        pasta = pasta / nome_pasta(f"{subpasta} - {api}")
    return (pasta / f"{nome_arquivo(nome)}.pdf").as_posix()


def ler_csv(caminho):
    try:
        with open(caminho, encoding="utf-8-sig", newline="") as arquivo:
            leitor = csv.DictReader(arquivo, delimiter=DELIMITADOR)
            colunas = {chave(c): c for c in leitor.fieldnames or []}
            faltando = [c for c in ("JORNADA", "API", "ART-TYPE", "ALLOWED", COLUNA_ID) if chave(c) not in colunas]
            if faltando:
                sys.exit(f"Faltam colunas no CSV: {', '.join(faltando)}.")
            brutas = list(leitor)
    except FileNotFoundError:
        sys.exit(f"CSV não encontrado: {caminho}")
    except UnicodeDecodeError:
        sys.exit("O CSV precisa estar em UTF-8 (no Excel: Salvar como > CSV UTF-8).")
    except csv.Error as exc:
        sys.exit(f"CSV mal formado: {exc}")

    linhas = []
    api_anterior = ("", "", 0)  
    for numero, bruta in enumerate(brutas, 1):
        def valor(coluna):
            return (bruta.get(colunas[chave(coluna)]) or "").strip()

        texto_api, page_id = valor("API"), valor(COLUNA_ID)
        tipo = re.sub(r"[\s_]+", "-", valor("ART-TYPE").upper())
        allowed = valor("ALLOWED").upper()
        if eh_metodo(texto_api) and tipo in ("API", "MS", "APID"):
            tipo += "-MTD"  
        linha = {"numero": numero, "id": page_id.lstrip("0"), "tipo": tipo, "destino": "", "motivo": "",
                 "aviso": "", "situacao": "invalida"}

        if tipo == "API":
            
            
            
            jornada, api = valor("JORNADA"), texto_api
            valida = bool(nome_pasta(jornada) and nome_pasta(api))
            api_anterior = (jornada, api, numero) if valida else ("", "", numero)
        else:
            jornada, api, linha_da_api = api_anterior
        
        
        nome = texto_api
        if not nome_arquivo(nome):
            nome = page_id.lstrip("0") if tipo.endswith("-MTD") else api
            if tipo.endswith("-MTD"):
                linha["aviso"] = "coluna API vazia; o arquivo usa o ID da página"

        if allowed in ("FALSE", "FALSO", "NAO", "NÃO", "0"):
            linha.update(situacao="ignorada", motivo="ALLOWED=FALSE")
        elif allowed not in ("TRUE", "VERDADEIRO", "SIM", "1"):
            linha["motivo"] = f"ALLOWED deve ser TRUE ou FALSE, veio '{valor('ALLOWED')}'"
        elif tipo not in TIPOS:
            linha["motivo"] = f"ART-TYPE desconhecido: '{valor('ART-TYPE')}'"
        elif not re.fullmatch(r"[0-9]+", page_id) or not page_id.strip("0"):
            linha["motivo"] = f"{COLUNA_ID} inválido: '{page_id}'" if page_id else f"{COLUNA_ID} vazio"
        elif tipo == "API" and not nome_pasta(jornada):
            linha["motivo"] = "JORNADA vazia"
        elif tipo == "API" and not nome_pasta(api):
            linha["motivo"] = "API vazia"
        elif not api:
            linha["motivo"] = (f"a linha de API anterior (linha {linha_da_api}) é inválida" if linha_da_api else "nenhuma linha de API antes desta no CSV")
        else:
            linha.update(situacao="valida", destino=destino_pdf(jornada, api, tipo, nome))
        linhas.append(linha)

    
    ids_por_destino = {}
    for linha in linhas:
        if linha["situacao"] == "valida":
            ids_por_destino.setdefault(linha["destino"].casefold(), set()).add(linha["id"])
    for linha in linhas:
        if linha["situacao"] == "valida" and len(ids_por_destino[linha["destino"].casefold()]) > 1:
            caminho = PurePosixPath(linha["destino"])
            linha["destino"] = caminho.with_name(f"{caminho.stem} ({linha['id']}).pdf").as_posix()
    return linhas


def caminho_longo(caminho):
    texto = str(Path(caminho).resolve())
    if os.name != "nt" or texto.startswith("\\\\?\\"):
        return Path(texto)
    if texto.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + texto[2:])
    return Path("\\\\?\\" + texto)


def pdf_integro(caminho):
    try:
        with open(caminho, "rb") as arquivo:
            inicio = arquivo.read(5)
            arquivo.seek(0, os.SEEK_END)
            tamanho = arquivo.tell()
            arquivo.seek(max(0, tamanho - 2048))
            fim = arquivo.read().rstrip(b"\x00\t\n\x0c\r ")
    except OSError:
        return False
    return inicio == b"%PDF-" and fim.endswith(b"%%EOF")


def metadados_pdf(caminho):
    caminho = Path(caminho)
    sha256 = hashlib.sha256()
    with open(caminho, "rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(64 * 1024), b""):
            sha256.update(bloco)

    page_count = 0
    page_title = ""
    try:
        reader = PdfReader(str(caminho), strict=False)
        page_count = len(reader.pages)
        metadata = reader.metadata
        if metadata:
            page_title = str(metadata.title or "").strip()
        if not page_title and reader.pages:
            texto = reader.pages[0].extract_text() or ""
            primeira_linha = next((linha.strip() for linha in texto.splitlines() if linha.strip()), "")
            page_title = primeira_linha[:500]
    except (OSError, PdfReadError, ValueError, TypeError, KeyError) as exc:
        log.warning("Não foi possível extrair metadados de %s: %s", caminho, exc)

    return {
        "pageTitle": page_title,
        "pageCount": page_count,
        "artifact": {
            "format": "pdf",
            "sizeBytes": caminho.stat().st_size,
            "sha256": sha256.hexdigest(),
        },
    }


def registrar_log_execucao(documento):
    try:
        mycol.insert_one(documento)
    except pymongo.errors.PyMongoError as exc:
        log.warning("Não foi possível registrar o log no MongoDB: %s", exc)


def copiar(origem, destino):
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo_temporario(destino.parent)
    try:
        shutil.copyfile(origem, temporario)
        os.replace(temporario, destino)
    finally:
        temporario.unlink(missing_ok=True)


def arquivo_temporario(pasta):
    descritor, nome = tempfile.mkstemp(dir=pasta, suffix=".part")
    os.close(descritor)
    return Path(nome)


def ler_estado(caminho):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gravar_estado(caminho, estado):
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(estado, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(temporario, caminho)


def criar_sessao(pat, ca_bundle):
    truststore.inject_into_ssl()
    sessao = requests.Session()
    
    
    sessao.trust_env = False
    sessao.headers.update({
        "Authorization": f"Bearer {pat}",   
        "X-Atlassian-Token": "no-check",    
        "Accept": "application/pdf, application/json, text/html;q=0.9, */*;q=0.8",
    })
    sessao.max_redirects = 3
    sessao.verify = ca_bundle or True
    return sessao


def explicar_erro_tls(exc):
    texto = str(exc).lower()
    if "hostname" in texto or "match" in texto or "not valid for" in texto:
        return "o certificado do servidor é de outro endereço. Confira CONFLUENCE_BASE_URL."
    if "expired" in texto:
        return "o certificado do servidor está vencido. Avise a equipe que mantém o Confluence."
    return ("o certificado do servidor não é confiável neste computador. Se o navegador abre o Confluence "
            "sem aviso, exporte a CA da empresa em formato PEM (Base-64) e informe o arquivo em CONFLUENCE_CA_BUNDLE.")


def segundos_retry_after(resposta):
    valor = resposta.headers.get("Retry-After", "").strip()
    if valor.isdigit():
        return float(valor)
    if valor:
        try:
            return max(0.0, parsedate_to_datetime(valor).timestamp() - time.time())
        except (TypeError, ValueError):
            return 0.0
    try:
        intervalo = float(resposta.headers["X-RateLimit-Interval-Seconds"])
        reposicao = float(resposta.headers["X-RateLimit-FillRate"])
        return intervalo / reposicao if intervalo > 0 and reposicao > 0 else 0.0
    except (KeyError, ValueError):
        return 0.0


def aguardar(segundos=0.0):
    global proxima_requisicao
    proxima_requisicao = max(proxima_requisicao, time.monotonic() + segundos)
    restante = proxima_requisicao - time.monotonic()
    if restante > 0:
        time.sleep(restante)


def liberar_proxima(segundos=None):
    global proxima_requisicao
    proxima_requisicao = time.monotonic() + (INTERVALO_MINIMO if segundos is None else segundos)


def pedir(sessao, url, stream=False, tentativas=None):
    global requisicoes
    problema = ""
    for tentativa in range(1, TENTATIVAS + 1):
        aguardar()
        backoff = INTERVALO_MINIMO * 2 ** (tentativa - 1)
        inicio_tentativa = time.monotonic()
        requisicao_iniciada = datetime.now(timezone.utc)
        numero_tentativa = len(tentativas) + 1 if tentativas is not None else tentativa
        try:
            
            resposta = sessao.get(url, stream=stream, timeout=TIMEOUT, verify=sessao.verify)
        except requests.exceptions.SSLError:
            requisicoes += 1
            if tentativas is not None:
                tentativas.append({
                    "number": numero_tentativa,
                    "requestedAt": requisicao_iniciada,
                    "httpStatus": None,
                    "durationMs": round((time.monotonic() - inicio_tentativa) * 1000),
                })
            raise
        except requests.TooManyRedirects:
            requisicoes += sessao.max_redirects + 1
            liberar_proxima()
            if tentativas is not None:
                tentativas.append({
                    "number": numero_tentativa,
                    "requestedAt": requisicao_iniciada,
                    "httpStatus": None,
                    "durationMs": round((time.monotonic() - inicio_tentativa) * 1000),
                })
            raise requests.TooManyRedirects(f"o Confluence redirecionou mais de {sessao.max_redirects} vezes") from None
        except (requests.ConnectionError, requests.Timeout) as exc:
            requisicoes += 1
            problema = "tempo esgotado" if isinstance(exc, requests.Timeout) else "falha de conexão"
            log.debug("Tentativa %s/%s sem resposta: %s", tentativa, TENTATIVAS, exc)
            if tentativas is not None:
                tentativas.append({
                    "number": numero_tentativa,
                    "requestedAt": requisicao_iniciada,
                    "httpStatus": None,
                    "durationMs": round((time.monotonic() - inicio_tentativa) * 1000),
                })
            liberar_proxima(backoff)
            continue
        requisicoes += 1 + len(resposta.history)
        log.debug("GET %s -> HTTP %s (tentativa %s/%s)", url, resposta.status_code, tentativa, TENTATIVAS)
        if tentativas is not None:
            tentativas.append({
                "number": numero_tentativa,
                "requestedAt": requisicao_iniciada,
                "httpStatus": resposta.status_code,
                "durationMs": round((time.monotonic() - inicio_tentativa) * 1000),
            })
        if resposta.status_code in (408, 429) or resposta.status_code >= 500:
            espera = segundos_retry_after(resposta)
            limites = {k: v for k, v in resposta.headers.items() if k.lower().startswith(("retry-after", "x-ratelimit"))}
            if limites:
                log.debug("Cabeçalhos de limite: %s", limites)
            resposta.close()
            if espera > MAX_RETRY_AFTER:
                raise RuntimeError(f"o Confluence pediu {espera:.0f} s de espera (HTTP {resposta.status_code})")
            problema = f"HTTP {resposta.status_code}"
            liberar_proxima(max(backoff, espera))
            continue
        liberar_proxima()
        return resposta
    raise requests.ConnectionError(f"sem resposta válida após {TENTATIVAS} tentativas ({problema})")


def eh_login(url):
    return urlsplit(url).path.lower().rstrip("/").endswith(("/login.action", "/dologin.action", "/login.jsp"))


def exportar_pdf(sessao, base_url, page_id, destino, tentativas=None):

    url = f"{base_url}/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}"
    return salvar_resposta(sessao, base_url, page_id, pedir(sessao, url, stream=True, tentativas=tentativas), destino, tarefa=True, tentativas=tentativas)


def salvar_resposta(sessao, base_url, page_id, resposta, destino, tarefa, tentativas=None):
    with resposta:
        if resposta.status_code == 401 or eh_login(resposta.url):
            return "autenticacao", "PAT recusado (HTTP 401 ou tela de login)."
        if resposta.status_code == 403:
            return "sem_permissao", "Sem permissão para exportar a página (HTTP 403)."
        if resposta.status_code == 404:
            return "nao_encontrada", "Página não encontrada (HTTP 404)."
        if resposta.status_code != 200:
            return "erro", f"O Confluence respondeu HTTP {resposta.status_code}."
        if parse_qs(urlsplit(resposta.url).query).get("pageId", [page_id]) != [page_id]:
            return "erro", "O Confluence redirecionou para outra página."

        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario = arquivo_temporario(destino.parent)
        try:
            limite = TAMANHO_MAXIMO_MB * 1024 * 1024
            tamanho = 0
            with open(temporario, "wb") as arquivo:
                for bloco in resposta.iter_content(64 * 1024):
                    tamanho += len(bloco)
                    if tamanho > limite:
                        return "erro", f"PDF maior que {TAMANHO_MAXIMO_MB} MB."
                    arquivo.write(bloco)
            liberar_proxima()  

            with open(temporario, "rb") as arquivo:
                inicio = arquivo.read(256 * 1024)
            if not inicio.startswith(b"%PDF-"):
                html = inicio.decode("utf-8", errors="replace")
                if re.search(r'type=["\']?password', html, re.I) and re.search(r"login|os_username", html, re.I):
                    return "autenticacao", "PAT recusado (o Confluence devolveu a tela de login)."
                tarefa_id = re.search(r'<meta[^>]+name=["\']ajs-taskid["\'][^>]+content=["\']([\w-]{1,128})["\']', html, re.I)
                if tarefa and tarefa_id:
                    return aguardar_tarefa(sessao, base_url, page_id, tarefa_id.group(1), destino, tentativas)
                return "erro", "O Confluence devolveu uma página HTML em vez do PDF."
            if not pdf_integro(temporario):
                return "erro", "PDF incompleto (download cortado)."
            os.replace(temporario, destino)
            return "ok", ""
        finally:
            temporario.unlink(missing_ok=True)


def aguardar_tarefa(sessao, base_url, page_id, tarefa_id, destino, tentativas=None):
    url = f"{base_url}/api/v2/pdfexporttask/progress/{quote(tarefa_id, safe='')}"
    contexto = urlsplit(base_url).path.rstrip("/")
    for consulta in range(CONSULTAS_TAREFA):
        aguardar(min(3 * 2 ** consulta, 30))
        with pedir(sessao, url, tentativas=tentativas) as resposta:
            if resposta.status_code == 401 or eh_login(resposta.url):
                return "autenticacao", "PAT recusado ao consultar a geração do PDF."
            if resposta.status_code != 200:
                return "erro", f"A consulta da geração do PDF respondeu HTTP {resposta.status_code}."
            try:
                progresso = resposta.json()
            except ValueError:
                return "erro", "Resposta inesperada ao consultar a geração do PDF."
        if "FAILED" in (str(progresso.get("state", "")).upper(), str(progresso.get("status", "")).upper()):
            return "erro", "O Confluence não conseguiu gerar o PDF."
        resultado = progresso.get("result")
        if isinstance(resultado, str) and resultado:
            if resultado.startswith("/") and contexto and not resultado.startswith(contexto + "/"):
                resultado = contexto + resultado
            alvo = urljoin(base_url + "/", resultado)
            if urlsplit(alvo).netloc != urlsplit(base_url).netloc or urlsplit(alvo).scheme != "https":
                return "erro", "O Confluence indicou um download em outro endereço."
            return salvar_resposta(sessao, base_url, page_id,
                                   pedir(sessao, alvo, stream=True, tentativas=tentativas),
                                   destino, tarefa=False, tentativas=tentativas)
    return "erro", f"O PDF não ficou pronto após {CONSULTAS_TAREFA} consultas."


def configurar_log(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    arquivo = logging.FileHandler(pasta / "execucao.log", encoding="utf-8")
    arquivo.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(console)
    log.addHandler(arquivo)


def ler_env():
    valores = {**dotenv_values(ARQUIVO_ENV, interpolate=False), **os.environ}
    base_url = (valores.get("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    pat = (valores.get("CONFLUENCE_PAT") or "").strip()
    ca_bundle = (valores.get("CONFLUENCE_CA_BUNDLE") or "").strip()
    if not base_url.startswith("https://"):
        sys.exit(f"Defina CONFLUENCE_BASE_URL (https://...) em {ARQUIVO_ENV}.")
    if not pat or any(c.isspace() for c in pat):
        sys.exit(f"Defina CONFLUENCE_PAT em {ARQUIVO_ENV}.")
    if ca_bundle and not Path(ca_bundle).is_file():
        sys.exit(f"CONFLUENCE_CA_BUNDLE não encontrado: {ca_bundle}")
    return base_url, pat, ca_bundle


def mostrar_verificacao(linhas):
    pasta = Path(PASTA_SAIDA).resolve()
    for linha in linhas:
        if linha["situacao"] == "valida":
            tamanho = len(str(pasta)) + 1 + len(linha["destino"])
            aviso = f"  (caminho com {tamanho} caracteres)" if tamanho >= LIMITE_CAMINHO_WINDOWS else ""
            if linha["aviso"]:
                aviso += f"  ({linha['aviso']})"
            log.info("Linha %s: %s%s", linha["numero"], linha["destino"], aviso)
        else:
            rotulo = "ignorada" if linha["situacao"] == "ignorada" else "inválida"
            log.info("Linha %s: %s (%s)", linha["numero"], rotulo, linha["motivo"])
    ids = {linha["id"] for linha in linhas if linha["situacao"] == "valida"}
    log.info("%s linhas, %s páginas a exportar. Nenhuma requisição foi feita.", len(linhas), len(ids))


def exportar_csv(sessao, base_url, linhas, pasta, contagem, run_id):
    estado_arquivo = pasta / "estado.json"
    estado = ler_estado(estado_arquivo)
    por_id = {}
    for linha in linhas:
        if linha["situacao"] == "valida":
            por_id.setdefault(linha["id"], []).append(linha)

    falhas_de_rede = 0
    motivo_parada = ""
    total = len(por_id)
    for numero, (page_id, linhas_id) in enumerate(por_id.items(), 1):
        principal = caminho_longo(pasta / linhas_id[0]["destino"])
        rotulo = f"[{numero}/{total}] {linhas_id[0]['tipo']} {page_id}"
        iniciado_em = time.monotonic()
        started_at = datetime.now(timezone.utc)
        tentativas = []

        if pdf_integro(principal):
            situacao, mensagem = "existente", ""
        elif page_id in estado:
            situacao, mensagem = estado[page_id], "resultado de uma execução anterior"
        else:
            try:
                situacao, mensagem = exportar_pdf(sessao, base_url, page_id, principal, tentativas)
                falhas_de_rede = 0
            except requests.exceptions.SSLError as exc:
                log.debug("Erro TLS: %s", exc)
                motivo_parada = "Certificado TLS recusado: " + explicar_erro_tls(exc)
                situacao, mensagem = "erro", "certificado TLS recusado"
            except RuntimeError as exc:
                motivo_parada = f"Execução interrompida: {exc}."
                situacao, mensagem = "erro", str(exc)
            except requests.TooManyRedirects as exc:  
                situacao, mensagem = "erro", str(exc)
            except requests.RequestException as exc:
                log.debug("Falha de rede: %s", exc)
                falhas_de_rede += 1
                situacao, mensagem = "erro", str(exc)
                if falhas_de_rede >= FALHAS_DE_REDE_SEGUIDAS:
                    motivo_parada = f"{falhas_de_rede} páginas seguidas sem resposta. Confira a VPN e a URL."
            except OSError as exc:
                tamanho = len(str(principal).removeprefix("\\\\?\\"))
                situacao, mensagem = "erro", f"não foi possível gravar o arquivo ({exc.strerror}; caminho com {tamanho} caracteres)"

        if situacao in ("ok", "existente"):
            contagem["baixados" if situacao == "ok" else "existentes"] += 1
            for outra in linhas_id[1:]:
                copia = caminho_longo(pasta / outra["destino"])
                try:
                    if not pdf_integro(copia):
                        copiar(principal, copia)
                        contagem["copias"] += 1
                except OSError as exc:
                    log.warning("  Não foi possível copiar para %s: %s", outra["destino"], exc.strerror)
            texto = "baixado" if situacao == "ok" else "já existia"
            log.info("%s %s: %s", rotulo, texto, linhas_id[0]["destino"])
        else:
            contagem[situacao if situacao in contagem else "erros"] += 1
            log.warning("%s falhou: %s", rotulo, mensagem)
            if situacao in ("sem_permissao", "nao_encontrada") and page_id not in estado:
                estado[page_id] = situacao
                gravar_estado(estado_arquivo, estado)
            if situacao == "autenticacao":
                motivo_parada = "O PAT foi recusado. Gere um novo PAT, atualize o .env e rode de novo."

        documento = {
            "runId": run_id,
            "pageId": page_id,
            "pageTitle": "",
            "status": "downloaded" if situacao == "ok" else ("existing" if situacao == "existente" else situacao),
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc),
            "durationMs": round((time.monotonic() - iniciado_em) * 1000),
            "destination": linhas_id[0]["destino"],
            "attempts": tentativas,
            "inputRows": [linha["numero"] for linha in linhas_id],
        }
        if situacao in ("ok", "existente") and pdf_integro(principal):
            metadados = metadados_pdf(principal)
            documento["pageTitle"] = metadados["pageTitle"]
            documento["pageCount"] = metadados["pageCount"]
            documento["artifact"] = metadados["artifact"]
        else:
            documento["error"] = mensagem
        registrar_log_execucao(documento)

        if motivo_parada:
            log.error(motivo_parada)
            break
    return motivo_parada


def main():
    pasta = Path(PASTA_SAIDA)
    configurar_log(pasta)
    inicio = time.monotonic()
    run_id = str(uuid.uuid4())

    if PAGINA_TESTE:
        base_url, pat, ca_bundle = ler_env()
        destino = caminho_longo(pasta / "teste" / f"{PAGINA_TESTE}.pdf")
        tentativas = []
        iniciado_em = time.monotonic()
        started_at = datetime.now(timezone.utc)
        try:
            situacao, mensagem = exportar_pdf(criar_sessao(pat, ca_bundle), base_url, PAGINA_TESTE, destino, tentativas)
        except requests.exceptions.SSLError as exc:
            log.debug("Erro TLS: %s", exc)
            situacao, mensagem = "erro", "certificado TLS recusado: " + explicar_erro_tls(exc)
        except (requests.RequestException, RuntimeError) as exc:
            situacao, mensagem = "erro", str(exc)
        documento = {
            "runId": run_id,
            "pageId": PAGINA_TESTE,
            "pageTitle": "",
            "status": "downloaded" if situacao == "ok" else situacao,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc),
            "durationMs": round((time.monotonic() - iniciado_em) * 1000),
            "destination": (pasta / "teste" / f"{PAGINA_TESTE}.pdf").as_posix(),
            "attempts": tentativas,
            "inputRows": [],
        }
        if situacao == "ok" and pdf_integro(destino):
            metadados = metadados_pdf(destino)
            documento["pageTitle"] = metadados["pageTitle"]
            documento["pageCount"] = metadados["pageCount"]
            documento["artifact"] = metadados["artifact"]
        else:
            documento["error"] = mensagem
        registrar_log_execucao(documento)
        if situacao == "ok":
            log.info("Página %s: PDF salvo em %s", PAGINA_TESTE, pasta / "teste" / f"{PAGINA_TESTE}.pdf")
        else:
            log.warning("Página %s falhou: %s", PAGINA_TESTE, mensagem)
        return 0 if situacao == "ok" else 1

    linhas = ler_csv(CSV_ENTRADA)
    log.info("=== %s linhas lidas de %s", len(linhas), CSV_ENTRADA)
    if APENAS_VERIFICAR:
        mostrar_verificacao(linhas)
        return 0
    for linha in linhas:
        if linha["situacao"] == "invalida":
            log.warning("Linha %s ignorada: %s", linha["numero"], linha["motivo"])
        elif linha["aviso"]:
            log.warning("Linha %s: %s", linha["numero"], linha["aviso"])

    base_url, pat, ca_bundle = ler_env()
    contagem = dict.fromkeys(("baixados", "existentes", "copias", "sem_permissao", "nao_encontrada", "erros"), 0)
    try:
        motivo_parada = exportar_csv(criar_sessao(pat, ca_bundle), base_url, linhas, pasta, contagem, run_id)
    except KeyboardInterrupt:
        motivo_parada = "Interrompido pelo usuário. Rode de novo para continuar."
        log.warning(motivo_parada)

    minutos = (time.monotonic() - inicio) / 60

    ignoradas = sum(linha["situacao"] == "ignorada" for linha in linhas)
    
    invalidas = sum(linha["situacao"] == "invalida" for linha in linhas)
    
    log.info(
            "=== Fim em %.1f min: %s baixados, %s já existiam, %s cópias locais, %s sem permissão, "
            "%s não encontrados, %s com erro | %s linhas ALLOWED=FALSE, %s inválidas | %s requisições HTTP.",
            minutos, contagem["baixados"], contagem["existentes"], contagem["copias"],
            contagem["sem_permissao"], contagem["nao_encontrada"], contagem["erros"],
            ignoradas, invalidas, requisicoes)
    
    falhou = motivo_parada or invalidas or contagem["sem_permissao"] or contagem["nao_encontrada"] or contagem["erros"]
    
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
