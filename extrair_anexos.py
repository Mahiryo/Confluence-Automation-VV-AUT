from email.utils import parsedate_to_datetime
import html
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import unquote, urljoin, urlsplit

try:
    import requests
    import truststore
    from dotenv import dotenv_values
except ImportError:
    sys.exit("Instale: python -m pip install -r requirements.txt")


PAGE_ID = "123456789"              
PASTA_SAIDA = "saida_anexos"       
ARQUIVO_ENV = ".env"               
EXTENSOES = (".json", ".yaml", ".yml")
SO_PAGINA_DE_ANEXOS = False        


INTERVALO_MINIMO = 3               
TENTATIVAS = 3                     
MAX_RETRY_AFTER = 120              
TIMEOUT = (10, 60)                 
TAMANHO_MAXIMO_MB = 50             
MAX_PAGINAS_LISTAGEM = 20          

log = logging.getLogger("extrator")
proxima_requisicao = 0.0           
requisicoes = 0                    

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


def criar_sessao(pat, ca_bundle):
    truststore.inject_into_ssl()   
    sessao = requests.Session()
    sessao.trust_env = False       
    sessao.headers.update({"Authorization": f"Bearer {pat}", "Accept": "application/json, */*;q=0.8"})
    sessao.max_redirects = 3
    sessao.verify = ca_bundle or True
    return sessao


def explicar_erro_tls(exc):
    texto = str(exc).lower()
    if "hostname" in texto or "match" in texto or "not valid for" in texto:
        return "o certificado do servidor é de outro endereço. Confira CONFLUENCE_BASE_URL."
    if "expired" in texto:
        return "o certificado do servidor está vencido."
    return ("o certificado do servidor não é confiável neste computador. Exporte a CA da empresa "
            "em formato PEM (Base-64) e informe o arquivo em CONFLUENCE_CA_BUNDLE.")


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


def aguardar():
    restante = proxima_requisicao - time.monotonic()
    if restante > 0:
        time.sleep(restante)


def liberar_proxima(segundos=None):
    global proxima_requisicao
    proxima_requisicao = time.monotonic() + (INTERVALO_MINIMO if segundos is None else segundos)


def pedir(sessao, url, stream=False):
    global requisicoes
    problema = ""
    for tentativa in range(1, TENTATIVAS + 1):
        aguardar()
        backoff = INTERVALO_MINIMO * 2 ** (tentativa - 1)
        try:
            resposta = sessao.get(url, stream=stream, timeout=TIMEOUT, verify=sessao.verify)
        except requests.exceptions.SSLError:
            requisicoes += 1
            raise
        except requests.TooManyRedirects:
            requisicoes += sessao.max_redirects + 1
            liberar_proxima()
            raise requests.TooManyRedirects(f"o Confluence redirecionou mais de {sessao.max_redirects} vezes") from None
        except (requests.ConnectionError, requests.Timeout) as exc:
            requisicoes += 1
            problema = "tempo esgotado" if isinstance(exc, requests.Timeout) else "falha de conexão"
            log.debug("Tentativa %s/%s sem resposta: %s", tentativa, TENTATIVAS, exc)
            liberar_proxima(backoff)
            continue
        requisicoes += 1 + len(resposta.history)
        log.debug("GET %s -> HTTP %s (tentativa %s/%s)", url, resposta.status_code, tentativa, TENTATIVAS)
        if resposta.status_code in (408, 429) or resposta.status_code >= 500:
            espera = segundos_retry_after(resposta)
            limites = {k: v for k, v in resposta.headers.items() if k.lower().startswith(("retry-after", "x-ratelimit"))}
            if limites:
                log.info("  HTTP %s; cabeçalhos de limite: %s", resposta.status_code, limites)
            resposta.close()
            if espera > MAX_RETRY_AFTER:
                raise RuntimeError(f"o Confluence pediu {espera:.0f} s de espera (HTTP {resposta.status_code})")
            problema = f"HTTP {resposta.status_code}"
            liberar_proxima(max(backoff, espera))
            continue
        liberar_proxima()
        return resposta
    raise requests.ConnectionError(f"sem resposta válida após {TENTATIVAS} tentativas ({problema})")


def eh_login(resposta):
    caminho = urlsplit(resposta.url).path.lower().rstrip("/")
    return resposta.status_code == 401 or caminho.endswith(("/login.action", "/dologin.action", "/login.jsp"))


def mesmo_endereco(base_url, link):
    contexto = urlsplit(base_url).path.rstrip("/")
    if link.startswith("/") and contexto and not link.startswith(contexto + "/"):
        link = contexto + link
    url = urljoin(base_url + "/", link)
    origem, destino = urlsplit(base_url), urlsplit(url)
    return url if (destino.scheme, destino.netloc) == (origem.scheme, origem.netloc) else None


def extensao_ok(nome):
    return nome.lower().endswith(EXTENSOES)


def listar_pela_api_rest(sessao, base_url, page_id):
    url = f"{base_url}/rest/api/content/{page_id}/child/attachment?limit=100&expand=version"
    anexos = []
    for _ in range(MAX_PAGINAS_LISTAGEM):
        with pedir(sessao, url) as resposta:
            if eh_login(resposta):
                raise PermissionError("PAT recusado (HTTP 401 ou tela de login).")
            if resposta.status_code in (403, 404):
                raise FileNotFoundError(f"página sem permissão ou inexistente (HTTP {resposta.status_code})")
            if resposta.status_code != 200:
                raise ValueError(f"a API REST respondeu HTTP {resposta.status_code}")
            try:
                dados = resposta.json()
            except ValueError:
                raise ValueError("a API REST não devolveu JSON") from None
        for item in dados.get("results", []):
            nome = item.get("title", "")
            link = (item.get("_links") or {}).get("download", "")
            if extensao_ok(nome) and link:
                versao = item.get("version") or {}
                anexos.append({"nome": nome, "link": link, "versao": versao.get("number", ""),
                               "data": versao.get("when", ""), "id": item.get("id", "")})
        proxima = (dados.get("_links") or {}).get("next")
        if not proxima:
            return anexos
        url = mesmo_endereco(base_url, proxima)
        if not url:
            raise ValueError("a paginação apontou para outro endereço")
    log.warning("  Listagem parou em %s páginas (MAX_PAGINAS_LISTAGEM); pode haver mais anexos.", MAX_PAGINAS_LISTAGEM)
    return anexos


def listar_pela_pagina_de_anexos(sessao, base_url, page_id):
    url = f"{base_url}/pages/viewpageattachments.action?pageId={page_id}"
    with pedir(sessao, url) as resposta:
        if eh_login(resposta):
            raise PermissionError("PAT recusado (HTTP 401 ou tela de login).")
        if resposta.status_code != 200:
            raise FileNotFoundError(f"a página de anexos respondeu HTTP {resposta.status_code}")
        texto = resposta.text
    if re.search(r'type=["\']?password', texto, re.I) and re.search(r"os_username|login", texto, re.I):
        raise PermissionError("PAT recusado (o Confluence devolveu a tela de login).")
    anexos, vistos = [], set()
    padrao = rf'href=["\']([^"\']*/download/attachments/{re.escape(str(page_id))}/([^?#"\']+))'
    for link, nome_codificado in re.findall(padrao, texto):
        nome = unquote(html.unescape(nome_codificado))
        
        if extensao_ok(nome) and nome.casefold() not in vistos:
            vistos.add(nome.casefold())
            anexos.append({"nome": nome, "link": html.unescape(link), "versao": "", "data": "", "id": ""})
    return anexos


def nome_arquivo(nome):
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", nome).strip(" .")
    return nome or "anexo"


def parece_ser(conteudo):
    texto = conteudo[:200_000].decode("utf-8-sig", errors="replace")
    try:
        dados = json.loads(texto)
    except ValueError:
        dados = None
    if isinstance(dados, dict):
        if "openapi" in dados:
            return f"OpenAPI {dados['openapi']} (JSON)"
        if "swagger" in dados:
            return f"Swagger {dados['swagger']} (JSON)"
        if "postman" in str((dados.get("info") or {}).get("schema", "")).lower():
            return "coleção do Postman (JSON)"
        return "JSON"
    versao = re.search(r"^(openapi|swagger)\s*:\s*['\"]?([\w.]+)", texto, re.M)
    if versao:
        return f"{'OpenAPI' if versao.group(1) == 'openapi' else 'Swagger'} {versao.group(2)} (YAML)"
    if texto.lstrip().lower().startswith(("<!doctype html", "<html")):
        return "HTML (provavelmente não é o anexo)"
    return "texto/YAML"


def baixar(sessao, base_url, anexo, pasta):
    url = mesmo_endereco(base_url, anexo["link"])
    if not url:
        return False, "o link de download aponta para outro endereço"
    with pedir(sessao, url, stream=True) as resposta:
        if eh_login(resposta):
            raise PermissionError("PAT recusado (HTTP 401 ou tela de login).")
        if resposta.status_code != 200:
            return False, f"o download respondeu HTTP {resposta.status_code}"
        destino = pasta / nome_arquivo(anexo["nome"])
        pasta.mkdir(parents=True, exist_ok=True)
        descritor, temporario = tempfile.mkstemp(dir=pasta, suffix=".part")
        os.close(descritor)
        temporario = Path(temporario)
        try:
            tamanho, limite = 0, TAMANHO_MAXIMO_MB * 1024 * 1024
            with open(temporario, "wb") as arquivo:
                for bloco in resposta.iter_content(64 * 1024):
                    tamanho += len(bloco)
                    if tamanho > limite:
                        return False, f"arquivo maior que {TAMANHO_MAXIMO_MB} MB"
                    arquivo.write(bloco)
            liberar_proxima()  
            inicio = temporario.read_bytes()[:256 * 1024]
            if re.search(rb'type=["\']?password', inicio, re.I) and re.search(rb"os_username|login", inicio, re.I):
                raise PermissionError("PAT recusado (o Confluence devolveu a tela de login).")
            os.replace(temporario, destino)
            legivel = f"{tamanho} bytes" if tamanho < 1024 else f"{tamanho / 1024:.0f} KB"
            return True, f"{legivel}, parece {parece_ser(inicio)}"
        finally:
            temporario.unlink(missing_ok=True)


def configurar_log(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    arquivo = logging.FileHandler(pasta / "extracao.log", encoding="utf-8")
    arquivo.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(console)
    log.addHandler(arquivo)


def main():
    pasta = Path(PASTA_SAIDA)
    configurar_log(pasta)
    page_id = PAGE_ID.strip().lstrip("0")
    if not page_id.isdigit():
        log.error("PAGE_ID deve conter só dígitos: '%s'", PAGE_ID)
        return 2
    base_url, pat, ca_bundle = ler_env()
    sessao = criar_sessao(pat, ca_bundle)
    destino = pasta / page_id
    inicio = time.monotonic()
    log.info("=== Anexos %s da página %s", "/".join(EXTENSOES), page_id)

    try:
        anexos = None
        if not SO_PAGINA_DE_ANEXOS:
            try:
                anexos = listar_pela_api_rest(sessao, base_url, page_id)
                log.info("Lista pela API REST: %s anexo(s).", len(anexos))
            except (ValueError, requests.ConnectionError, RuntimeError) as exc:
                log.warning("A API REST falhou (%s). Tentando a página de anexos.", exc)
        if anexos is None:
            anexos = listar_pela_pagina_de_anexos(sessao, base_url, page_id)
            log.info("Lista pela página de anexos: %s anexo(s).", len(anexos))

        baixados = 0
        for numero, anexo in enumerate(anexos, 1):
            detalhes = ", ".join(x for x in (f"versão {anexo['versao']}" if anexo["versao"] else "", anexo["data"]) if x)
            try:
                ok, mensagem = baixar(sessao, base_url, anexo, destino)
            except (requests.ConnectionError, RuntimeError) as exc:
                ok, mensagem = False, str(exc)
            baixados += ok
            situacao = "baixado" if ok else "falhou"
            log.info("[%s/%s] %s%s: %s (%s)", numero, len(anexos), anexo["nome"],
                     f" ({detalhes})" if detalhes else "", situacao, mensagem)
    except PermissionError as exc:
        log.error("%s Gere um novo PAT, atualize o .env e rode de novo.", exc)
        return 1
    except FileNotFoundError as exc:
        log.error("Não foi possível listar os anexos: %s.", exc)
        return 1
    except requests.exceptions.SSLError as exc:
        log.debug("Erro TLS: %s", exc)
        log.error("Certificado TLS recusado: %s", explicar_erro_tls(exc))
        return 1
    except (requests.RequestException, RuntimeError) as exc:
        log.error("Falha de rede: %s.", exc)
        return 1

    log.info("=== Fim em %.1f s: %s de %s anexo(s) salvos em %s | %s requisições HTTP.",
             time.monotonic() - inicio, baixados, len(anexos), destino, requisicoes)
    return 0 if baixados == len(anexos) else 1


if __name__ == "__main__":
    sys.exit(main())