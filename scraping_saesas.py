# -*- coding: utf-8 -*-
"""
Scraping SAESAS - saesas.gov.co
Extrae inmuebles en arriendo en Medellin, detecta cambios y notifica por email.
Funciona local y en GitHub Actions.
"""

import requests, json, re, os, smtplib, time, urllib3, socket, sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# Suprimir warnings de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Forzar IPv4 (GitHub Actions falla con IPv6 en saesas.gov.co)
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(*args, **kwargs):
    return _orig_getaddrinfo(*args, **kwargs, family=socket.AF_INET) if len(args) < 3 else _orig_getaddrinfo(*args, **kwargs)

# Parche mas limpio: forzar AF_INET
import urllib3.util.connection as urllib3_cn
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACION
# ═══════════════════════════════════════════════════════════════════════════════

COL_TZ = timezone(timedelta(hours=-5))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATOS_FILE   = os.path.join(SCRIPT_DIR, "datos_anteriores_saesas.json")
CAMBIOS_FILE = os.path.join(SCRIPT_DIR, "registro_cambios_saesas.json")

BASE_URL = "https://www.saesas.gov.co"
LISTING_URL = (
    BASE_URL
    + "/transparencia-y-acceso-a-la-informacion-publica"
    "/5-tramites-y-servicios/arriendos/arrendamientos"
)
REGION_MEDELLIN = "05001"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
}

# Email
EMAIL_REMITENTE    = os.environ.get("EMAIL_REMITENTE", "cuanticateamsas@gmail.com")
EMAIL_CONTRASENA   = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO", "cuanticateamsas@gmail.com")

DIAS_ROJO = 2


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SCRAPING - LISTADO
# ═══════════════════════════════════════════════════════════════════════════════

def obtener_links_listado():
    """Recorre todas las paginas del listado y extrae links a detalles."""
    links = []
    pagina = 1

    while True:
        params = {"region": REGION_MEDELLIN, "page": pagina}
        print(f"  Pagina {pagina}...")

        for intento in range(3):
            try:
                resp = requests.get(
                    LISTING_URL, params=params, headers=HEADERS,
                    verify=False, timeout=30
                )
                resp.raise_for_status()
                break
            except Exception:
                if intento < 2:
                    time.sleep(3)
                else:
                    raise

        soup = BeautifulSoup(resp.text, "html.parser")

        # Buscar links a detalle de inmuebles
        encontrados = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/arriendos/arrendamientos/" in href and href != LISTING_URL:
                # Excluir el link del breadcrumb (que apunta solo a /arrendamientos)
                slug = href.rstrip("/").split("/")[-1]
                if slug and slug != "arrendamientos":
                    full_url = href if href.startswith("http") else BASE_URL + href
                    if full_url not in links:
                        links.append(full_url)
                        encontrados += 1

        if encontrados == 0:
            break

        # Verificar si hay pagina siguiente
        next_link = soup.find("a", string=re.compile(r"Next"))
        if not next_link:
            break

        pagina += 1

    return links


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SCRAPING - DETALLE DE CADA INMUEBLE
# ═══════════════════════════════════════════════════════════════════════════════

def extraer_nombre(descripcion, titulo):
    """Extrae el nombre del lugar/edificio de la descripcion del inmueble."""
    texto = descripcion or ""

    # Palabras genericas que NO son nombres propios de edificios
    GENERICAS = {
        "en estructura", "tradicional", "propiedad horizontal",
        "uso residencial", "uso comercial", "uso mixto",
        "comercial y residencial", "residencial y comercial",
        "ubicado en", "sector de", "el cual consta",
    }

    # Patrones para nombres de lugares en la descripcion
    # Cada patron captura: (prefijo completo, nombre propio)
    # Terminadores: palabras comunes que indican fin del nombre propio
    TERM = r"(?:\s*[,.]|\s+Propiedad|\s+Cuenta|\s+Ubicad|\s+Con\b|\s+Se\b|\s+El\s+Cual|\s+Barrio|\s+Ph\b|\s+P\.H)"
    patrones = [
        rf"(Centro\s+Comercial)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Edificio|Ed\.)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ]+(?:\s+[A-ZÀ-Ü][A-Za-zÀ-üñÑ]+)*){TERM}",
        rf"(Condominio)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Conjunto\s+(?:Residencial\s+|Cerrado\s+)?)\s*([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Urbanizaci[oó]n|Urb\.?)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Torre)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ]+(?:\s+[A-ZÀ-Ü][A-Za-zÀ-üñÑ]+)*){TERM}",
        rf"(Unidad\s+Residencial)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Mall)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Plaza)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
        rf"(Hotel)\s+([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?){TERM}",
    ]

    for pat in patrones:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            prefijo = m.group(1).strip()
            nombre = m.group(2).strip()
            # Filtrar nombres genericos
            if any(g in nombre.lower() for g in GENERICAS):
                continue
            if len(nombre) < 2:
                continue
            nombre_completo = f"{prefijo} {nombre}".strip()
            nombre_completo = re.sub(r"\s+", " ", nombre_completo)
            if 4 < len(nombre_completo) < 60:
                return nombre_completo.title()

    # Fallback: buscar ubicacion en la descripcion (barrio, sector, comuna)
    ubicacion_pats = [
        r"(?:ubicad[oa]?\s+en\s+(?:el\s+)?(?:barrio|sector)\s+)([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?)(?:\s+de\s+la\s+comuna|\s+del?\s+municipio|\s*[,.])",
        r"(?:barrio\s+)([A-ZÀ-Ü][A-Za-zÀ-üñÑ\s]+?)(?:\s+de\s+la\s+comuna|\s+del?\s+municipio|\s*[,.])",
        r"(?:sector\s+(?:de\s+)?)([A-ZÀ-Ü][A-Za-zÀ-üñÑ]+(?:\s+[A-ZÀ-Ü][A-Za-zÀ-üñÑ]+)*)(?:\s*[,.]|\s+de\b|\s+del\b|\s+el\b)",
    ]
    for pat in ubicacion_pats:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            ubicacion = m.group(1).strip()
            if (len(ubicacion) > 2
                and ubicacion.lower() not in ("el", "la", "del")
                and not any(g in ubicacion.lower() for g in GENERICAS)):
                return ubicacion.title()

    # Fallback titulo: nombre despues de "FMI xxx"
    if titulo:
        # "Apartamento FMI 001-574744  Condominio Orion" -> "Condominio Orion"
        m = re.search(r"FMI\s+[\w-]+\s{2,}(.+?)$", titulo)
        if not m:
            m = re.search(r"FMI\s+[\w-]+\s+([A-Z][a-zA-ZÀ-üñÑ\s.]+)$", titulo)
        if m:
            nombre = m.group(1).strip()
            if (len(nombre) > 2
                and not re.match(r"^UE\s", nombre, re.IGNORECASE)
                and not re.match(r"^/", nombre)
                and not re.match(r"^\d", nombre)):
                return nombre

    # Fallback titulo: nombre entre tipo y FMI/codigo
    if titulo:
        # "Casa Quintas de San Luis FMI xxx" -> "Quintas de San Luis"
        tipos_re = r"(?:Casa|Apartamento|Apartaestudio|Local\s+Comercial|Oficina|Bodega|Edificio|Hotel|Edificacion|Parqueadero|Local|Apt)"
        m = re.search(rf"^{tipos_re}\s+(.+?)\s*(?:FMI|-FMI|$)", titulo, re.IGNORECASE)
        if m:
            nombre = m.group(1).strip()
            # Limpiar sufijos como "- 001-xxx"
            nombre = re.sub(r"\s*-\s*[\d][\d\-/]+.*$", "", nombre).strip()
            if (len(nombre) > 2
                and not re.match(r"^[\d\-\s]+$", nombre)
                and not re.match(r"^0\d[NnSs]", nombre)
                and not re.match(r"^UE\s", nombre, re.IGNORECASE)
                and not re.match(r"^-\s", nombre)
                and not re.match(r"^de\s+recreo", nombre, re.IGNORECASE)):
                return nombre

    return ""


def scrape_detalle(url, idx, total):
    """Extrae datos de la pagina de detalle de un inmueble."""
    for intento in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, verify=False, timeout=30)
            resp.raise_for_status()
            break
        except Exception:
            if intento < 2:
                time.sleep(3)
            else:
                print(f"    ERROR: no se pudo cargar {url}")
                return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extraer campos del bloque de detalles
    def buscar_campo(label):
        tag = soup.find("strong", string=re.compile(label, re.IGNORECASE))
        if tag and tag.parent:
            texto = tag.parent.get_text(separator=" ", strip=True)
            # Quitar el label
            valor = re.sub(re.escape(tag.get_text()), "", texto, count=1).strip()
            # Limpiar ":" al inicio
            valor = valor.lstrip(":").strip()
            return valor
        return ""

    tipo = buscar_campo(r"Tipo inmueble")
    fmi = buscar_campo(r"FMI")
    direccion = buscar_campo(r"Direcci[oó]n")
    area_terreno = buscar_campo(r"[AÁ]rea terreno")
    area_construida = buscar_campo(r"[AÁ]rea construida")
    municipio = buscar_campo(r"Municipio")

    # Usar area construida si > 0, sino area terreno
    area = area_construida or area_terreno
    # Limpiar "0 mts²" -> preferir la otra
    if area and re.match(r"^0\s", area):
        area = area_terreno if area == area_construida else area_construida
    if area and re.match(r"^0\s", area):
        area = "0 mts²"

    # Precio
    precio = ""
    precio_tag = soup.find("p", class_=re.compile(r"text-.*pink|text-.*bold"))
    if precio_tag:
        precio = precio_tag.get_text(strip=True)
    if not precio:
        p_label = soup.find("p", string=re.compile(r"Precio de arriendo", re.IGNORECASE))
        if p_label and p_label.find_next_sibling("p"):
            precio = p_label.find_next_sibling("p").get_text(strip=True)

    # Titulo (h1)
    titulo = ""
    h1 = soup.find("h1")
    if h1:
        titulo = h1.get_text(strip=True)

    # Si no encontro tipo, extraerlo del titulo
    if not tipo and titulo:
        tipo = titulo.split("-")[0].strip() if "-" in titulo else titulo

    # Descripcion del inmueble
    descripcion = ""
    h2_desc = soup.find("h2", string=re.compile(r"Descripci[oó]n del inmueble", re.IGNORECASE))
    if h2_desc:
        h5 = h2_desc.find_next_sibling("h5")
        if h5:
            descripcion = h5.get_text(strip=True)

    # Extraer nombre del lugar/edificio de la descripcion
    nombre = extraer_nombre(descripcion, titulo)

    slug = url.rstrip("/").split("/")[-1]
    print(f"  [{idx}/{total}] {titulo or slug} -> {nombre}")

    return {
        "_id": slug,
        "nombre": nombre,
        "tipo": tipo,
        "direccion": direccion,
        "area": area,
        "fmi": fmi,
        "precio": precio,
        "link": url,
        "titulo": titulo,
        "municipio": municipio,
    }


def scrape_todos(links):
    """Scrape detalle de todos los inmuebles."""
    inmuebles = []
    total = len(links)
    for i, url in enumerate(links, 1):
        datos = scrape_detalle(url, i, total)
        if datos:
            inmuebles.append(datos)
        time.sleep(0.5)  # cortesia
    return inmuebles


# ═══════════════════════════════════════════════════════════════════════════════
# 3. DETECCION DE CAMBIOS
# ═══════════════════════════════════════════════════════════════════════════════

CAMPOS_COMPARAR = ["tipo", "direccion", "area", "fmi", "precio"]


def cargar_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detectar_cambios(inmuebles):
    anteriores = cargar_json(DATOS_FILE)
    registro = cargar_json(CAMBIOS_FILE)
    ahora = datetime.now(COL_TZ).isoformat()
    limite = (datetime.now(COL_TZ) - timedelta(days=DIAS_ROJO)).isoformat()
    resumen = []
    cambios_ahora = {}

    ids_actuales = set()

    for item in inmuebles:
        pid = item["_id"]
        ids_actuales.add(pid)
        prev = anteriores.get(pid, {})

        # Inmueble NUEVO
        if not prev:
            resumen.append({
                "tipo_cambio": "NUEVO",
                "titulo": item.get("titulo", "?"),
                "tipo": item.get("tipo", ""),
                "precio": item.get("precio", ""),
                "link": item.get("link", ""),
            })
            for campo in CAMPOS_COMPARAR:
                registro[f"{pid}:{campo}"] = ahora
                cambios_ahora[f"{pid}:{campo}"] = True

        # Cambios en campos
        for campo in CAMPOS_COMPARAR:
            nuevo = str(item.get(campo, ""))
            viejo = str(prev.get(campo, ""))
            if prev and nuevo != viejo:
                registro[f"{pid}:{campo}"] = ahora
                cambios_ahora[f"{pid}:{campo}"] = True
                resumen.append({
                    "tipo_cambio": "CAMBIO",
                    "titulo": item.get("titulo", "?"),
                    "campo": campo, "antes": viejo, "ahora": nuevo,
                })

        anteriores[pid] = {c: str(item.get(c, "")) for c in CAMPOS_COMPARAR}
        anteriores[pid]["titulo"] = item.get("titulo", "")

    # Inmuebles ELIMINADOS
    for clave in list(anteriores.keys()):
        if clave not in ids_actuales:
            resumen.append({
                "tipo_cambio": "ELIMINADO",
                "titulo": anteriores[clave].get("titulo", "?"),
                "tipo": anteriores[clave].get("tipo", ""),
            })
            del anteriores[clave]

    # Limpiar cambios viejos
    for k in list(registro.keys()):
        if registro[k] < limite:
            del registro[k]

    guardar_json(DATOS_FILE, anteriores)
    guardar_json(CAMBIOS_FILE, registro)
    return cambios_ahora, resumen


# ═══════════════════════════════════════════════════════════════════════════════
# 4. EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

def enviar_email(resumen):
    if not EMAIL_CONTRASENA or not resumen:
        print("  Email desactivado (sin contrasena) o sin cambios")
        return

    try:
        nuevos = [c for c in resumen if c["tipo_cambio"] == "NUEVO"]
        eliminados = [c for c in resumen if c["tipo_cambio"] == "ELIMINADO"]
        cambios = [c for c in resumen if c["tipo_cambio"] == "CAMBIO"]

        fecha = datetime.now(COL_TZ).strftime("%d/%m/%Y %H:%M")

        partes = []
        if nuevos: partes.append(f"{len(nuevos)} nuevos")
        if eliminados: partes.append(f"{len(eliminados)} eliminados")
        if cambios: partes.append(f"{len(cambios)} cambios")
        asunto = f"SAESAS Arriendos Medellin - {', '.join(partes)}"

        ESTILO_TABLA = (
            "border-collapse:collapse;width:100%;font-family:Arial,sans-serif;"
            "font-size:13px;margin-bottom:20px;"
        )
        ESTILO_TH = (
            "background-color:#6E1978;color:white;padding:8px 12px;"
            "text-align:left;border:1px solid #ccc;"
        )
        ESTILO_TD = "padding:8px 12px;border:1px solid #ddd;"
        ESTILO_TD_ALT = ESTILO_TD + "background-color:#f8f8f8;"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;">
        <h2 style="color:#6E1978;margin-bottom:5px;">Alerta - SAESAS Arriendos Medellin</h2>
        <p style="color:#666;margin-top:0;">{fecha}</p>
        """

        # NUEVOS
        if nuevos:
            html += f'<h3 style="color:#2E7D32;">Nuevos inmuebles ({len(nuevos)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Tipo</th>'
            html += f'<th style="{ESTILO_TH}">Precio</th>'
            html += f'<th style="{ESTILO_TH}">Link</th></tr>'
            for i, c in enumerate(nuevos):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                link_html = f'<a href="{c.get("link","")}" style="color:#6E1978;">Ver</a>'
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c.get("tipo","")}</td>'
                html += f'<td style="{td}">{c.get("precio","")}</td>'
                html += f'<td style="{td}">{link_html}</td></tr>'
            html += '</table>'

        # CAMBIOS
        if cambios:
            html += f'<h3 style="color:#1565C0;">Cambios detectados ({len(cambios)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Campo</th>'
            html += f'<th style="{ESTILO_TH}">Antes</th>'
            html += f'<th style="{ESTILO_TH}">Ahora</th></tr>'
            for i, c in enumerate(cambios):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c["campo"]}</td>'
                html += f'<td style="{td}">{c.get("antes","") or "-"}</td>'
                html += f'<td style="{td}">{c.get("ahora","") or "-"}</td></tr>'
            html += '</table>'

        # ELIMINADOS
        if eliminados:
            html += f'<h3 style="color:#C62828;">Inmuebles eliminados ({len(eliminados)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Tipo</th></tr>'
            for i, c in enumerate(eliminados):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c.get("tipo","")}</td></tr>'
            html += '</table>'

        html += """
        <p style="color:#999;font-size:12px;margin-top:20px;">
        Tabla actualizada en Google Sheets<br>
        Fuente: saesas.gov.co
        </p></div>
        """

        msg = MIMEMultipart()
        msg["From"] = EMAIL_REMITENTE
        msg["To"] = EMAIL_DESTINATARIO
        msg["Subject"] = asunto
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.send_message(msg)
        print(f"  Email enviado a {EMAIL_DESTINATARIO}")
    except Exception as e:
        print(f"  Error enviando email: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  Scraping SAESAS - saesas.gov.co")
    print("  Inmuebles en arriendo - Medellin")
    print("=" * 55)

    try:
        # Obtener links del listado
        print("\n[1/4] Obteniendo listado de inmuebles...")
        links = obtener_links_listado()
        print(f"  {len(links)} inmuebles encontrados")

        # Scrape detalle
        print("\n[2/4] Extrayendo detalles de cada inmueble...")
        inmuebles = scrape_todos(links)
        print(f"  {len(inmuebles)} inmuebles procesados")

        # Detectar cambios
        print("\n[3/4] Detectando cambios...")
        cambios, resumen = detectar_cambios(inmuebles)

        if resumen:
            print(f"\n  *** {len(resumen)} CAMBIOS DETECTADOS ***")
            for c in resumen[:15]:
                if c["tipo_cambio"] == "CAMBIO":
                    print(f"    - {c['titulo']}: {c['campo']} {c.get('antes','')} -> {c.get('ahora','')}")
                else:
                    print(f"    - {c['tipo_cambio']} {c['titulo']}")
            if len(resumen) > 15:
                print(f"    ... y {len(resumen)-15} mas")
        else:
            print("  Sin cambios respecto a la ultima actualizacion")

        # Google Sheets
        print("\n[4/4] Actualizando Google Sheets...")
        from sheets_sync_saesas import sync_to_sheets
        sync_to_sheets(inmuebles, cambios)

        # Email
        if resumen:
            enviar_email(resumen)

        print(f"\nListo! {len(inmuebles)} inmuebles en arriendo en Medellin")

    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
        print(f"\nSitio saesas.gov.co no disponible: {e}")
        print("  El sitio puede estar caido o bloqueando conexiones.")
        print("  Se reintentara en la proxima ejecucion programada.")
        sys.exit(0)
